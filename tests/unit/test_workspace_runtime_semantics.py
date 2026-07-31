from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest

from serena_light.lsp.adapter import (
    AdapterGenerations,
    AdapterPhase,
    AdapterSnapshot,
    CrashSnapshot,
    DerivedToolAvailability,
    DocumentReadinessProbe,
    DocumentReadinessTarget,
    EngineMetadata,
    GlobalReadinessWitness,
    RawLspProviders,
)
from serena_light.lsp.normalize import Location
from serena_light.lsp.positions import FileSnapshot, PositionEncoding
from serena_light.lsp.state import DiagnosticsSnapshot, DiagnosticsState
from serena_light.tools.compact_adapter import compact_navigation_result
from serena_light.tools.editing import NotificationResult, ReplacementNotification
from serena_light.tools.envelopes import ErrorEnvelope, GenerationMetadata
from serena_light.tools.references import ReferenceTarget
from serena_light.workspace.identity import (
    LocationKind,
    SemanticLocation,
    WorkspaceError,
    WorkspaceErrorCode,
    WorkspaceErrorData,
    WorkspaceIdentity,
    WorkspaceKind,
)
from serena_light.workspace.inventory import SupportedPathTree, TrustInventory
from serena_light.workspace.runtime import AdapterBuildContext, AdapterFactory, WorkspaceRuntime
from serena_light.workspace.scope import LanguageFamily, NativeProgramAttribution, ProjectKind, ScopeProjection


class _Policy:
    def __init__(self) -> None:
        self.edit_calls: list[Path] = []
        self.failure: WorkspaceError | None = None
        self.read_only_external_paths: set[Path] = set()

    def authorize_path_operand(self, identity: WorkspaceIdentity, path: str | Path, inventory: Sequence[Path]) -> Path:
        if self.failure is not None:
            raise self.failure
        resolved = Path(path).resolve(strict=True)
        if resolved not in {item.resolve(strict=True) for item in inventory}:
            raise ValueError("not in inventory")
        assert resolved.is_relative_to(identity.root)
        return resolved

    def authorize_edit(self, identity: WorkspaceIdentity, path: str | Path, inventory: Sequence[Path]) -> Path:
        self.edit_calls.append(Path(path))
        return self.authorize_path_operand(identity, path, inventory)

    def classify_semantic_location(self, identity: WorkspaceIdentity, path: str | Path) -> SemanticLocation:
        resolved = Path(path).resolve(strict=True)
        if resolved in self.read_only_external_paths:
            return SemanticLocation(resolved, LocationKind.READ_ONLY_EXTERNAL)
        if resolved.is_relative_to(identity.root):
            return SemanticLocation(resolved, LocationKind.WORKSPACE)
        raise ValueError("untrusted semantic location")


class _Client:
    def __init__(self, replies: Mapping[str, object]) -> None:
        self.replies = replies
        self.requests: list[tuple[str, object]] = []
        self.notifications: list[tuple[str, object]] = []

    def request(self, method: str, params: object = None, *, timeout: float | None = None) -> object:
        del timeout
        self.requests.append((method, params))
        reply = self.replies.get(method)
        return cast(Callable[[], object], reply)() if callable(reply) else reply

    def notify(self, method: str, params: object = None) -> None:
        self.notifications.append((method, params))

    def shutdown(self, *, timeout: float = 2.0) -> None:
        del timeout


class _Adapter:
    def __init__(
        self,
        context: AdapterBuildContext,
        replies: Mapping[str, object],
        phase: AdapterPhase,
        raw_providers: RawLspProviders,
    ) -> None:
        self.context = context
        self.client: _Client = _Client(replies)
        self.phase = phase
        self.raw_providers = raw_providers
        self.document_generation = 0
        self.document_loads: list[str] = []
        self.edit_dispatches = 0
        self.edit_epilogue: Callable[[], None] | None = None
        self.diagnostics = DiagnosticsSnapshot(DiagnosticsState.MISSING, "", None, None, 0, None, ())
        # A replacement transport/process after a restart keeps every other
        # generation coincidentally identical; only this token distinguishes
        # it.  Defaults are unchanged so every other test is unaffected.
        self.runtime_token = 0
        self.position_encoding = PositionEncoding.UTF16

    def routes(self, path: str | Path) -> bool:
        suffix = PurePosixPath(str(path)).suffix.lower()
        return suffix in ({".py", ".pyi"} if self.context.family is LanguageFamily.PYTHON else {".ts"})

    def snapshot(self) -> AdapterSnapshot:
        return AdapterSnapshot(
            self.context.family.value,
            self.phase,
            self.raw_providers,
            DerivedToolAvailability.from_raw(self.raw_providers),
            EngineMetadata(
                "pyright" if self.context.family is LanguageFamily.PYTHON else "typescript",
                "1.0",
                Path("/owned/server"),
                Path("/owned/python") if self.context.family is LanguageFamily.PYTHON else None,
            ),
            self.position_encoding,
            AdapterGenerations(1, 2, self.document_generation, 3),
            CrashSnapshot(0, 0, None, None, None, 0.0),
            (),
            True,
            runtime_token=self.runtime_token,
        )

    def snapshot_open_and_probe_document(
        self,
        *,
        absolute_path: Path,
        relative_path: str,
        uri: str,
        version: int,
        probe: DocumentReadinessProbe,
    ) -> Future[tuple[FileSnapshot, DocumentReadinessTarget]]:
        def worker() -> tuple[FileSnapshot, DocumentReadinessTarget]:
            snapshot = FileSnapshot.from_bytes(absolute_path.read_bytes())
            self.document_loads.append(relative_path)
            self.document_generation += 1
            target = DocumentReadinessTarget(uri, relative_path, absolute_path, version, self.document_generation, 0)
            assert probe.observe(self.client, target, timeout=1.0)
            return snapshot, target

        return self.context.executor.submit(worker)

    def submit_read(self, operation: Callable[[_Client], Any]) -> Future[Any]:
        return self.context.executor.submit(lambda: operation(self.client))

    def submit_edit(self, operation: Callable[[_Client], Any]) -> Future[Any]:
        self.edit_dispatches += 1

        def run_edit() -> Any:
            result = operation(self.client)
            # Models a transport that loses the reply after the worker finished.
            if self.edit_epilogue is not None:
                self.edit_epilogue()
            return result

        return self.context.executor.submit(run_edit)

    def warm_global(
        self,
        witness: GlobalReadinessWitness,
        *,
        timeout: float | None = None,
    ) -> Future[tuple[Mapping[str, object], ...]]:
        del timeout

        def worker() -> tuple[Mapping[str, object], ...]:
            result = self.client.request("workspace/symbol", {"query": witness.query or witness.exact_symbol})
            assert isinstance(result, Sequence) and not isinstance(result, str | bytes)
            exact_items: list[Mapping[str, object]] = []
            for item in cast(Sequence[object], result):
                if isinstance(item, Mapping):
                    mapped = cast(Mapping[str, object], item)
                    location = mapped.get("location")
                    if (
                        mapped.get("name") == witness.exact_symbol
                        and isinstance(location, Mapping)
                        and cast(Mapping[str, object], location).get("uri") == witness.uri
                    ):
                        exact_items.append(mapped)
            exact = tuple(exact_items)
            assert exact
            self.phase = AdapterPhase.READY
            return exact

        return self.context.executor.submit(worker)

    def open_snapshot_document_with_client(
        self,
        client: _Client,
        *,
        absolute_path: Path,
        relative_path: str,
        uri: str,
        version: int,
        text: str,
    ) -> DocumentReadinessTarget:
        del client, text
        self.document_generation += 1
        self.document_loads.append(relative_path)
        return DocumentReadinessTarget(uri, relative_path, absolute_path, version, self.document_generation, 0)

    def open_edit_document_with_client(
        self,
        client: _Client,
        *,
        absolute_path: Path,
        relative_path: str,
        uri: str,
        version: int,
        text: str,
    ) -> DocumentReadinessTarget:
        return self.open_snapshot_document_with_client(
            client,
            absolute_path=absolute_path,
            relative_path=relative_path,
            uri=uri,
            version=version,
            text=text,
        )

    def notify_edit_with_client(
        self,
        client: _Client,
        target: DocumentReadinessTarget,
        notification: ReplacementNotification,
    ) -> NotificationResult:
        self.document_generation += 1
        client.notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": target.uri, "version": target.version + 1},
                "contentChanges": [{"text": notification.text}],
            },
        )
        return NotificationResult(
            "notified",
            self.document_generation,
            GenerationMetadata(trust=1, program=2, document=self.document_generation, index=3),
        )

    def diagnostics_snapshot(self, target: DocumentReadinessTarget) -> DiagnosticsSnapshot:
        if self.diagnostics.uri:
            return self.diagnostics
        return DiagnosticsSnapshot(
            DiagnosticsState.MISSING,
            target.uri,
            target.absolute_path,
            target.version,
            target.document_generation,
            None,
            (),
        )

    def stop(self) -> Future[AdapterSnapshot]:
        return self.context.executor.submit(self.snapshot)


def _symbol(name: str) -> dict[str, object]:
    return {
        "name": name,
        "kind": 12,
        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": len(name)}},
        "selectionRange": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": len(name)}},
    }


def _raw_range(start_line: int, start_character: int, end_line: int, end_character: int) -> dict[str, object]:
    return {
        "start": {"line": start_line, "character": start_character},
        "end": {"line": end_line, "character": end_character},
    }


def _inventory(root: Path, paths: tuple[str, ...]) -> TrustInventory:
    return TrustInventory(
        root=root.resolve(),
        paths=paths,
        rejected=(),
        digest=hashlib.sha256("\0".join(paths).encode()).hexdigest(),
        tree=SupportedPathTree.from_paths(paths),
        kind="test",
    )


def _projection(family: LanguageFamily, paths: tuple[str, ...]) -> ScopeProjection:
    return ScopeProjection.from_attribution(
        trust_inventory_paths=paths,
        attribution=NativeProgramAttribution(family, ProjectKind.WORKSPACE_DEFAULT, None, paths),
    )


def _runtime(
    tmp_path: Path,
    replies: Mapping[str, object],
    *,
    phase: AdapterPhase = AdapterPhase.READY,
    raw_providers: RawLspProviders | None = None,
    future_timeout: float = 35.0,
    attributors: Mapping[LanguageFamily, Callable[[Path, tuple[str, ...]], ScopeProjection]] | None = None,
) -> tuple[WorkspaceRuntime, _Adapter, _Policy]:
    paths = tuple(
        sorted(str(path.relative_to(tmp_path)) for extension in ("*.py", "*.ts") for path in tmp_path.rglob(extension))
    )
    adapters: list[_Adapter] = []
    providers = raw_providers or RawLspProviders(
        definition=True,
        implementation=True,
        references=True,
        document_symbols=True,
        workspace_symbols=True,
    )

    def build(context: AdapterBuildContext) -> _Adapter:
        adapter = _Adapter(context, replies, phase, providers)
        adapters.append(adapter)
        return adapter

    policy = _Policy()
    runtime = WorkspaceRuntime(
        (WorkspaceKind.GIT, tmp_path),
        path_policy=policy,
        inventory=_inventory(tmp_path, paths),
        attributors=attributors
        or {
            LanguageFamily.PYTHON: lambda _root, values: _projection(LanguageFamily.PYTHON, values),
            LanguageFamily.TYPESCRIPT: lambda _root, values: _projection(LanguageFamily.TYPESCRIPT, values),
        },
        adapter_factories={
            LanguageFamily.PYTHON: cast(AdapterFactory, build),
            LanguageFamily.TYPESCRIPT: cast(AdapterFactory, build),
        },
        future_timeout=future_timeout,
    )
    return runtime, adapters[0], policy


def test_declaration_uses_fixed_adapter_and_definition_request(tmp_path: Path) -> None:
    source = tmp_path / "src/main.py"
    source.parent.mkdir()
    source.write_text("target()\n")
    target = tmp_path / "src/target.py"
    target.write_text("def target(): pass\n")
    runtime, adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": [_symbol("target")],
            "textDocument/definition": {
                "uri": target.as_uri(),
                "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 6}},
            },
        },
    )
    try:
        result = runtime.find_declaration("src/main.py", r"(target)\(\)").to_dict()
        assert result["ok"] is True
        assert [method for method, _ in adapter.client.requests] == [
            "textDocument/documentSymbol",
            "textDocument/definition",
            "textDocument/definition",
        ]
        assert result["data"]["locations"][0]["relative_path"] == "src/target.py"
    finally:
        runtime.stop()


def test_typescript_declaration_uses_definition_when_declaration_provider_is_absent(tmp_path: Path) -> None:
    source = tmp_path / "src/main.ts"
    source.parent.mkdir()
    source.write_text("target();\n")
    target = tmp_path / "src/target.ts"
    target.write_text("// 😀\r\nconst target = 1;\r\n")
    symbol_calls = 0

    def document_symbols() -> list[Mapping[str, object]]:
        nonlocal symbol_calls
        symbol_calls += 1
        if symbol_calls == 1:
            return []
        return [
            {
                "name": "target",
                "kind": 13,
                "detail": "const target: number",
                "range": _raw_range(1, 0, 1, 17),
                "selectionRange": _raw_range(1, 6, 1, 12),
            }
        ]

    runtime, adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": document_symbols,
            "textDocument/definition": {
                "uri": target.as_uri(),
                "range": {"start": {"line": 1, "character": 6}, "end": {"line": 1, "character": 12}},
            },
        },
        raw_providers=RawLspProviders(definition=True, declaration=False, document_symbols=True),
    )
    try:
        result = runtime.find_declaration(
            "src/main.ts", r"(target)\(\)", include_body=True, include_info=True
        ).to_dict()

        assert result["ok"] is True
        assert [method for method, _ in adapter.client.requests] == [
            "textDocument/documentSymbol",
            "textDocument/definition",
            "textDocument/documentSymbol",
            "textDocument/definition",
        ]
        assert result["data"]["capabilities"]["raw"]["declarationProvider"] is False
        location = result["data"]["locations"][0]
        assert location["body"] == "target"
        assert location["range"]["start"] == {"line": 1, "column": 6, "text_offset": 12, "byte_offset": 15}
        assert location["name_path"] == "target"
        assert location["info"] == {"detail": "const target: number"}
    finally:
        runtime.stop()


def test_python_cross_library_declaration_uses_definition_not_declaration_provider(tmp_path: Path) -> None:
    source = tmp_path / "src/main.py"
    source.parent.mkdir()
    source.write_text("from transformers import GenerationConfig\n")
    external = tmp_path.parent / "site-packages" / "transformers" / "configuration_utils.py"
    external.parent.mkdir(parents=True, exist_ok=True)
    external.write_text("class GenerationConfig: ...\n")
    runtime, adapter, policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": [],
            "textDocument/definition": {
                "uri": external.as_uri(),
                "range": {"start": {"line": 0, "character": 6}, "end": {"line": 0, "character": 22}},
            },
        },
        raw_providers=RawLspProviders(definition=True, declaration=False, document_symbols=True),
    )
    policy.read_only_external_paths.add(external.resolve())
    try:
        result = runtime.find_declaration("src/main.py", r"import\s+(GenerationConfig)").to_dict()

        assert result["ok"] is True
        assert [method for method, _ in adapter.client.requests] == [
            "textDocument/documentSymbol",
            "textDocument/definition",
            "textDocument/definition",
        ]
        location = result["data"]["locations"][0]
        assert location["absolute_path"] == str(external.resolve())
        assert location["location_kind"] == "read_only_external"
        assert location["read_only_external"] is True
        assert "range" not in location
        assert location["raw_lsp_range"] == {
            "basis": "lsp_zero_based_line_utf16_code_unit_character",
            "start": {"line": 0, "character": 6},
            "end": {"line": 0, "character": 22},
        }
        assert result["data"]["capabilities"]["derived"]["find_declaration"] is True
    finally:
        runtime.stop()


def test_declaration_requires_definition_even_when_declaration_provider_is_present(tmp_path: Path) -> None:
    source = tmp_path / "src/main.py"
    source.parent.mkdir()
    source.write_text("target()\n")
    runtime, adapter, _policy = _runtime(
        tmp_path,
        {"textDocument/documentSymbol": []},
        raw_providers=RawLspProviders(definition=False, declaration=True, document_symbols=True),
    )
    try:
        result = runtime.find_declaration("src/main.py", r"(target)\(\)").to_dict()

        assert result["error"]["code"] == "UNSUPPORTED"
        assert result["error"]["details"]["capabilities"]["raw"] == {
            "definitionProvider": False,
            "declarationProvider": True,
            "implementationProvider": False,
            "referencesProvider": False,
            "documentSymbolProvider": True,
            "workspaceSymbolProvider": False,
        }
        assert adapter.client.requests == [("textDocument/documentSymbol", {"textDocument": {"uri": source.as_uri()}})]
    finally:
        runtime.stop()


def test_typescript_implementation_location_without_symbol_kind_survives_public_runtime(tmp_path: Path) -> None:
    source = tmp_path / "src/main.ts"
    source.parent.mkdir()
    source.write_text("interface Runner {}\n")
    implementation = tmp_path / "src/implementation.ts"
    implementation.write_text("class Impl implements Runner {}\n")
    runtime, adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": [_symbol("Runner")],
            "textDocument/implementation": {
                "uri": implementation.as_uri(),
                "range": {"start": {"line": 0, "character": 6}, "end": {"line": 0, "character": 10}},
            },
        },
        raw_providers=RawLspProviders(implementation=True, document_symbols=True),
    )
    try:
        result = runtime.find_implementations("Runner", "src/main.ts").to_dict()

        assert result["ok"] is True
        assert [method for method, _ in adapter.client.requests] == [
            "textDocument/documentSymbol",
            "textDocument/implementation",
            "textDocument/implementation",
        ]
        location = result["data"]["locations"][0]
        assert location["relative_path"] == "src/implementation.ts"
        assert "kind" not in location
    finally:
        runtime.stop()


@pytest.mark.parametrize(
    ("include_kinds", "exclude_kinds", "expected_count", "expected_omitted"),
    [
        ([5], None, 1, 0),
        (None, [5], 0, 1),
    ],
)
def test_implementation_kind_filters_use_response_owned_target_symbols(
    tmp_path: Path,
    include_kinds: list[int] | None,
    exclude_kinds: list[int] | None,
    expected_count: int,
    expected_omitted: int,
) -> None:
    source = tmp_path / "src/main.ts"
    source.parent.mkdir()
    source.write_text("interface Runner {}\n")
    implementation = tmp_path / "src/implementation.ts"
    implementation.write_text("class Impl implements Runner {}\n")
    symbol_calls = 0

    def document_symbols() -> list[Mapping[str, object]]:
        nonlocal symbol_calls
        symbol_calls += 1
        if symbol_calls == 1:
            return [_symbol("Runner")]
        return [
            {
                "name": "Impl",
                "kind": 5,
                "detail": "class Impl implements Runner",
                "range": _raw_range(0, 0, 0, 31),
                "selectionRange": _raw_range(0, 6, 0, 10),
            }
        ]

    runtime, adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": document_symbols,
            "textDocument/implementation": {
                "uri": implementation.as_uri(),
                "range": _raw_range(0, 6, 0, 10),
            },
        },
        raw_providers=RawLspProviders(implementation=True, document_symbols=True),
    )
    try:
        result = runtime.find_implementations(
            "Runner",
            "src/main.ts",
            include_kinds=include_kinds,
            exclude_kinds=exclude_kinds,
        ).to_dict()

        assert result["ok"] is True
        assert len(result["data"]["locations"]) == expected_count
        if expected_count:
            assert result["data"]["locations"][0]["kind"] == 5
        assert result["truncation"] == {
            "truncated": expected_omitted > 0,
            "omitted_count": expected_omitted,
        }
        assert [method for method, _ in adapter.client.requests] == [
            "textDocument/documentSymbol",
            "textDocument/implementation",
            "textDocument/documentSymbol",
            "textDocument/implementation",
        ]
    finally:
        runtime.stop()


def test_implementation_include_info_retains_response_owned_symbol_detail_in_compact_output(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src/main.ts"
    source.parent.mkdir()
    source.write_text("interface Runner {}\n")
    implementation = tmp_path / "src/implementation.ts"
    implementation.write_text("class Impl implements Runner {}\n")
    symbol_calls = 0

    def document_symbols() -> list[Mapping[str, object]]:
        nonlocal symbol_calls
        symbol_calls += 1
        if symbol_calls == 1:
            return [_symbol("Runner")]
        return [
            {
                "name": "Impl",
                "kind": 5,
                "detail": "class Impl implements Runner",
                "range": _raw_range(0, 0, 0, 31),
                "selectionRange": _raw_range(0, 6, 0, 10),
            }
        ]

    runtime, _adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": document_symbols,
            "textDocument/implementation": {
                "uri": implementation.as_uri(),
                "range": _raw_range(0, 6, 0, 10),
            },
        },
        raw_providers=RawLspProviders(implementation=True, document_symbols=True),
    )
    try:
        result = runtime.find_implementations("Runner", "src/main.ts", include_info=True).to_dict()

        assert result["ok"] is True
        location = result["data"]["locations"][0]
        assert location["name_path"] == "Impl"
        assert location["info"] == {"detail": "class Impl implements Runner"}
        assert "selection_range" not in location["info"]
        compact = compact_navigation_result("find_implementations", result)
        assert compact.structuredContent is not None
        files = cast(Mapping[str, object], compact.structuredContent["data"])["files"]
        assert cast(Sequence[Mapping[str, object]], files)[0]["targets"] == [
            {
                "range": [[0, 6], [0, 10]],
                "name_path": "Impl",
                "kind": "class",
                "info": "class Impl implements Runner",
            }
        ]
    finally:
        runtime.stop()


def test_declaration_target_change_between_response_and_snapshot_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "src/main.py"
    source.parent.mkdir()
    source.write_text("target()\n")
    target = tmp_path / "src/target.py"
    target.write_text("def target(): pass\n")
    calls = 0

    def changing_definition() -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            target.write_text("# moved\ndef target(): pass\n")
            line = 0
        else:
            line = 1
        return {
            "uri": target.as_uri(),
            "range": {"start": {"line": line, "character": 4}, "end": {"line": line, "character": 10}},
        }

    runtime, _adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": [_symbol("target")],
            "textDocument/definition": changing_definition,
        },
    )
    try:
        result = runtime.find_declaration("src/main.py", r"(target)\(\)", include_body=True).to_dict()

        assert result["error"]["code"] == "NOT_READY"
        assert result["error"]["retry"]["retryable"] is True
        assert result["error"]["details"]["reason"] == "semantic target locations changed"
        assert calls == 2
    finally:
        runtime.stop()


def test_declaration_self_target_replays_once_and_keeps_the_already_open_source_snapshot(tmp_path: Path) -> None:
    """A self-target reuses the open source snapshot, and a raced read replays.

    The write completes before this read's final guarded validation, so the
    pre-write snapshot must not escape.  Within each attempt the self-target
    still resolves from the already-open source document rather than a second
    document load.
    """

    source = tmp_path / "src/main.py"
    source.parent.mkdir()
    source.write_text("target()\n")
    calls = 0

    def stable_server_definition() -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            source.write_text("# moved\ntarget()\n")
        return {
            "uri": source.as_uri(),
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 6}},
        }

    runtime, adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": [_symbol("target")],
            "textDocument/definition": stable_server_definition,
        },
    )
    try:
        result = runtime.find_declaration("src/main.py", r"(target)\(\)", include_body=True).to_dict()

        assert result["ok"] is True
        # The returned body belongs to the settled bytes, not to the discarded
        # first attempt that observed "target()\n".
        assert result["data"]["locations"][0]["body"] == source.read_text()[:6] == "# move"
        # One source load per attempt: the self-target is never opened again.
        assert adapter.document_loads == ["src/main.py", "src/main.py"]
        # Two stabilization requests per attempt, across exactly two attempts.
        assert calls == 4
    finally:
        runtime.stop()


def test_declaration_generation_transition_during_authoritative_response_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "src/main.py"
    source.parent.mkdir()
    source.write_text("target()\n")
    target = tmp_path / "src/target.py"
    target.write_text("def target(): pass\n")
    calls = 0
    current_adapter: _Adapter | None = None

    def stable_definition_with_transition() -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            assert current_adapter is not None
            current_adapter.document_generation += 1
        return {
            "uri": target.as_uri(),
            "range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 10}},
        }

    runtime, adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": [_symbol("target")],
            "textDocument/definition": stable_definition_with_transition,
        },
    )
    current_adapter = adapter
    try:
        result = runtime.find_declaration("src/main.py", r"(target)\(\)", include_body=True).to_dict()

        assert result["error"]["code"] == "NOT_READY"
        assert result["error"]["retry"]["retryable"] is True
        assert result["error"]["details"]["reason"] == "semantic target generation changed"
        assert calls == 2
    finally:
        runtime.stop()


def test_implementation_target_change_between_response_and_snapshot_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "src/main.ts"
    source.parent.mkdir()
    source.write_text("interface Runner {}\n")
    target = tmp_path / "src/implementation.ts"
    target.write_text("class Impl implements Runner {}\n")
    calls = 0

    def changing_implementation() -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            target.write_text("// moved\nclass Impl implements Runner {}\n")
            line = 0
        else:
            line = 1
        return {
            "uri": target.as_uri(),
            "range": {"start": {"line": line, "character": 6}, "end": {"line": line, "character": 10}},
        }

    runtime, _adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": [_symbol("Runner")],
            "textDocument/implementation": changing_implementation,
        },
        raw_providers=RawLspProviders(implementation=True, document_symbols=True),
    )
    try:
        result = runtime.find_implementations("Runner", "src/main.ts", include_info=True).to_dict()

        assert result["error"]["code"] == "NOT_READY"
        assert result["error"]["retry"]["retryable"] is True
        assert result["error"]["details"]["reason"] == "semantic target locations changed"
        assert calls == 2
    finally:
        runtime.stop()


def test_declaration_replay_rejects_replacement_runtime_token_at_first_request(tmp_path: Path) -> None:
    """A restart during the first semantic response must never let a
    retained source snapshot ride through on a replacement process, even
    when every generation coincidentally matches the pre-restart adapter."""

    source = tmp_path / "src/main.py"
    source.parent.mkdir()
    source.write_text("target()\n")
    target = tmp_path / "src/target.py"
    target.write_text("def target(): pass\n")
    calls = 0
    current_adapter: _Adapter | None = None

    def restart_at_first_request() -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert current_adapter is not None
            current_adapter.runtime_token += 1
        return {
            "uri": target.as_uri(),
            "range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 10}},
        }

    runtime, adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": [_symbol("target")],
            "textDocument/definition": restart_at_first_request,
        },
    )
    current_adapter = adapter
    try:
        result = runtime.find_declaration("src/main.py", r"(target)\(\)", include_body=True).to_dict()

        assert result["error"]["code"] == "NOT_READY"
        assert result["error"]["retry"]["retryable"] is True
        assert result["error"]["details"]["reason"] == "semantic source adapter identity changed"
        assert calls == 1
        assert "src/target.py" not in adapter.document_loads
    finally:
        runtime.stop()


def test_declaration_replay_rejects_replacement_runtime_token_during_target_capture(tmp_path: Path) -> None:
    """A restart while opening the response-owned target snapshot must fail
    closed rather than render through a snapshot bound to a dead process."""

    source = tmp_path / "src/main.py"
    source.parent.mkdir()
    source.write_text("target()\n")
    target = tmp_path / "src/target.py"
    target.write_text("def target(): pass\n")
    calls = 0

    def stable_definition() -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        return {
            "uri": target.as_uri(),
            "range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 10}},
        }

    runtime, adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": [_symbol("target")],
            "textDocument/definition": stable_definition,
        },
    )
    original_open = adapter.open_snapshot_document_with_client

    def restart_during_capture(
        client: _Client,
        *,
        absolute_path: Path,
        relative_path: str,
        uri: str,
        version: int,
        text: str,
    ) -> DocumentReadinessTarget:
        adapter.runtime_token += 1
        return original_open(
            client,
            absolute_path=absolute_path,
            relative_path=relative_path,
            uri=uri,
            version=version,
            text=text,
        )

    adapter.open_snapshot_document_with_client = restart_during_capture  # type: ignore[method-assign]
    try:
        result = runtime.find_declaration("src/main.py", r"(target)\(\)", include_body=True).to_dict()

        assert result["error"]["code"] == "NOT_READY"
        assert result["error"]["retry"]["retryable"] is True
        assert result["error"]["details"]["reason"] == "semantic source adapter identity changed"
        # The target snapshot was captured before the identity mismatch was
        # detected; it must still never be rendered as authoritative.
        assert adapter.document_loads.count("src/target.py") == 1
        assert calls == 1
    finally:
        runtime.stop()


def test_declaration_replay_rejects_replacement_runtime_token_at_second_request(tmp_path: Path) -> None:
    """A restart between the bounded replay's two identical requests must
    fail closed instead of accepting the first response's retained target."""

    source = tmp_path / "src/main.py"
    source.parent.mkdir()
    source.write_text("target()\n")
    target = tmp_path / "src/target.py"
    target.write_text("def target(): pass\n")
    calls = 0
    current_adapter: _Adapter | None = None

    def restart_at_second_request() -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            assert current_adapter is not None
            current_adapter.runtime_token += 1
        return {
            "uri": target.as_uri(),
            "range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 10}},
        }

    runtime, adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": [_symbol("target")],
            "textDocument/definition": restart_at_second_request,
        },
    )
    current_adapter = adapter
    try:
        result = runtime.find_declaration("src/main.py", r"(target)\(\)", include_body=True).to_dict()

        assert result["error"]["code"] == "NOT_READY"
        assert result["error"]["retry"]["retryable"] is True
        assert result["error"]["details"]["reason"] == "semantic target generation changed"
        assert calls == 2
        assert adapter.document_loads.count("src/target.py") == 1
    finally:
        runtime.stop()


def test_declaration_replay_rejects_changed_capability_set_mid_transaction(tmp_path: Path) -> None:
    """A capability change between the two responses (e.g. a renegotiated
    initialize on restart) must fail closed even though the runtime token,
    phase, and generations otherwise still match."""

    source = tmp_path / "src/main.py"
    source.parent.mkdir()
    source.write_text("target()\n")
    target = tmp_path / "src/target.py"
    target.write_text("def target(): pass\n")
    calls = 0
    current_adapter: _Adapter | None = None

    def drop_capability_before_second_request() -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            assert current_adapter is not None
            current_adapter.raw_providers = RawLspProviders(definition=False, document_symbols=True)
        return {
            "uri": target.as_uri(),
            "range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 10}},
        }

    runtime, adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": [_symbol("target")],
            "textDocument/definition": drop_capability_before_second_request,
        },
    )
    current_adapter = adapter
    try:
        result = runtime.find_declaration("src/main.py", r"(target)\(\)", include_body=True).to_dict()

        assert result["error"]["code"] == "NOT_READY"
        assert result["error"]["retry"]["retryable"] is True
        assert result["error"]["details"]["reason"] == "semantic target generation changed"
        assert calls == 2
    finally:
        runtime.stop()


def test_declaration_replay_rejects_changed_position_encoding_mid_transaction(tmp_path: Path) -> None:
    """An encoding renegotiation between the two responses must fail closed
    rather than mix coordinate bases across the replay."""

    source = tmp_path / "src/main.py"
    source.parent.mkdir()
    source.write_text("target()\n")
    target = tmp_path / "src/target.py"
    target.write_text("def target(): pass\n")
    calls = 0
    current_adapter: _Adapter | None = None

    def change_encoding_before_second_request() -> Mapping[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            assert current_adapter is not None
            current_adapter.position_encoding = PositionEncoding.UTF32
        return {
            "uri": target.as_uri(),
            "range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 10}},
        }

    runtime, adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": [_symbol("target")],
            "textDocument/definition": change_encoding_before_second_request,
        },
    )
    current_adapter = adapter
    try:
        result = runtime.find_declaration("src/main.py", r"(target)\(\)", include_body=True).to_dict()

        assert result["error"]["code"] == "NOT_READY"
        assert result["error"]["retry"]["retryable"] is True
        assert result["error"]["details"]["reason"] == "semantic target generation changed"
        assert calls == 2
    finally:
        runtime.stop()


def test_declaration_replay_succeeds_when_runtime_identity_is_unchanged(tmp_path: Path) -> None:
    """A stable runtime token/capability/encoding identity throughout the
    bounded replay must still succeed; the new checks must not produce a
    false positive for ordinary same-process reuse."""

    source = tmp_path / "src/main.py"
    source.parent.mkdir()
    source.write_text("target()\n")
    target = tmp_path / "src/target.py"
    target.write_text("def target(): pass\n")
    runtime, adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": [_symbol("target")],
            "textDocument/definition": {
                "uri": target.as_uri(),
                "range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 10}},
            },
        },
    )
    # A non-zero but unchanging token models a process that has already
    # restarted once before this call, then stayed put for its duration.
    adapter.runtime_token = 7
    try:
        result = runtime.find_declaration("src/main.py", r"(target)\(\)", include_body=True).to_dict()

        assert result["ok"] is True
        assert result["data"]["locations"][0]["body"] == "target"
        assert [method for method, _ in adapter.client.requests].count("textDocument/definition") == 2
    finally:
        runtime.stop()


def test_global_find_symbol_only_loads_workspace_symbol_candidates(tmp_path: Path) -> None:
    candidate = tmp_path / "src/candidate.py"
    candidate.parent.mkdir()
    candidate.write_text("class Target: pass\n")
    (tmp_path / "src/unrelated.py").write_text("class Other: pass\n")
    runtime, adapter, _policy = _runtime(
        tmp_path,
        {
            "workspace/symbol": [
                {
                    "name": "Target",
                    "kind": 12,
                    "location": {"uri": candidate.as_uri(), "range": _symbol("Target")["range"]},
                }
            ],
            "textDocument/documentSymbol": [_symbol("Target")],
        },
    )
    try:
        result = runtime.find_symbol("Target").to_dict()
        assert result["ok"] is True
        assert adapter.document_loads == ["src/candidate.py"]
        assert "src/unrelated.py" not in adapter.document_loads
        assert [method for method, _ in adapter.client.requests] == ["workspace/symbol", "textDocument/documentSymbol"]
    finally:
        runtime.stop()


def test_python_assignment_body_is_recovered_through_runtime_with_snapshot_hash(tmp_path: Path) -> None:
    source = tmp_path / "answer.py"
    source_bytes = b"answer: int = (\n    40 +\n    2\n)\n"
    source.write_bytes(source_bytes)
    raw_symbol = {
        "name": "answer",
        "kind": 14,
        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 6}},
        "selectionRange": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 6},
        },
    }
    runtime, _adapter, _policy = _runtime(
        tmp_path,
        {"textDocument/documentSymbol": [raw_symbol]},
    )
    try:
        result = runtime.find_symbol("answer", relative_path="answer.py", include_body=True).to_dict()

        assert result["ok"] is True
        assert result["data"]["sha256"] == hashlib.sha256(source_bytes).hexdigest()
        assert result["data"]["symbol"]["body"] == "answer: int = (\n    40 +\n    2\n)"
        assert result["data"]["symbol"]["range"]["end"] == {
            "line": 3,
            "column": 1,
            "text_offset": len(source_bytes) - 1,
            "byte_offset": len(source_bytes) - 1,
        }
    finally:
        runtime.stop()


def test_python_ambiguous_assignment_body_fails_closed_through_runtime(tmp_path: Path) -> None:
    source = tmp_path / "ambiguous.py"
    source.write_text("x = 1\nx = 2\n")
    zero_width = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}
    raw_symbol = {
        "name": "x",
        "kind": 13,
        "range": zero_width,
        "selectionRange": zero_width,
    }
    runtime, _adapter, _policy = _runtime(
        tmp_path,
        {"textDocument/documentSymbol": [raw_symbol]},
    )
    try:
        ordinary = runtime.find_symbol("x", relative_path="ambiguous.py").to_dict()
        body = runtime.find_symbol("x", relative_path="ambiguous.py", include_body=True).to_dict()

        assert ordinary["ok"] is True
        assert ordinary["data"]["symbol"]["range"]["start"] == {
            "line": 0,
            "column": 0,
            "text_offset": 0,
            "byte_offset": 0,
        }
        assert body["error"]["code"] == "UNSUPPORTED"
        assert body["error"]["details"] == {
            "operation": "find_symbol",
            "reason": "incomplete_assignment_range",
            "recovery_reason": "ambiguous",
            "relative_path": "ambiguous.py",
            "name_path": "x",
        }
    finally:
        runtime.stop()


def test_python_assignment_name_mismatch_cannot_supply_body_or_edit_target(tmp_path: Path) -> None:
    source = tmp_path / "answers.py"
    original = b"good = 1\nother = 2\n"
    source.write_bytes(original)
    wrong_anchor = {
        "start": {"line": 1, "character": 0},
        "end": {"line": 1, "character": 5},
    }
    raw_symbol = {
        "name": "good",
        "kind": 14,
        "range": wrong_anchor,
        "selectionRange": wrong_anchor,
        "children": [],
    }
    runtime, adapter, _policy = _runtime(
        tmp_path,
        {"textDocument/documentSymbol": [raw_symbol]},
    )
    try:
        body = runtime.find_symbol("good", relative_path="answers.py", include_body=True).to_dict()
        edit = runtime.replace_symbol_body(
            "good",
            "answers.py",
            "good = 3",
            hashlib.sha256(original).hexdigest(),
        ).to_dict()

        assert body["error"]["code"] == "UNSUPPORTED"
        assert body["error"]["details"]["reason"] == "incomplete_assignment_range"
        assert body["error"]["details"]["recovery_reason"] == "no_enclosing_assignment"
        assert edit["error"]["code"] == "SYMBOL_NOT_FOUND"
        assert source.read_bytes() == original
        assert adapter.client.notifications == []
    finally:
        runtime.stop()


def test_typescript_destructured_body_uses_server_selection_range_chain(tmp_path: Path) -> None:
    source = tmp_path / "answer.ts"
    source.write_text("export const [tupleA, tupleB] = [1, 2];\n")
    first = {"start": {"line": 0, "character": 14}, "end": {"line": 0, "character": 20}}
    second = {"start": {"line": 0, "character": 22}, "end": {"line": 0, "character": 28}}
    binding = {"start": {"line": 0, "character": 13}, "end": {"line": 0, "character": 29}}
    statement = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 39}}
    symbols = [
        {"name": "tupleA", "kind": 14, "range": first, "selectionRange": first},
        {"name": "tupleB", "kind": 14, "range": second, "selectionRange": second},
    ]
    selection_ranges = [
        {"range": first, "parent": {"range": binding, "parent": {"range": statement}}},
        {"range": second, "parent": {"range": binding, "parent": {"range": statement}}},
    ]
    runtime, adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": symbols,
            "textDocument/selectionRange": selection_ranges,
        },
    )
    try:
        result = runtime.find_symbol(
            "tupleA", relative_path="answer.ts", include_body=True, include_info=True
        ).to_dict()

        assert result["ok"] is True
        assert result["data"]["symbol"]["body"] == "export const [tupleA, tupleB] = [1, 2];"
        assert result["data"]["symbol"]["info"]["selection_range"] == {
            "start": {"line": 0, "column": 14, "text_offset": 14, "byte_offset": 14},
            "end": {"line": 0, "column": 20, "text_offset": 20, "byte_offset": 20},
        }
        assert [method for method, _params in adapter.client.requests] == [
            "textDocument/documentSymbol",
            "textDocument/selectionRange",
        ]
    finally:
        runtime.stop()


def test_typescript_identifier_start_body_uses_complete_variable_statement(tmp_path: Path) -> None:
    source = tmp_path / "multiline.ts"
    source.write_text("export const multiline = (\n  1 +\n  2\n);\n")
    selection = {"start": {"line": 0, "character": 13}, "end": {"line": 0, "character": 22}}
    server_range = {"start": {"line": 0, "character": 13}, "end": {"line": 3, "character": 1}}
    statement = {"start": {"line": 0, "character": 0}, "end": {"line": 3, "character": 2}}
    runtime, adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": [
                {"name": "multiline", "kind": 14, "range": server_range, "selectionRange": selection}
            ],
            "textDocument/selectionRange": [{"range": selection, "parent": {"range": statement}}],
        },
    )
    try:
        result = runtime.find_symbol("multiline", relative_path="multiline.ts", include_body=True).to_dict()

        assert result["ok"] is True
        assert result["data"]["symbol"]["body"] == "export const multiline = (\n  1 +\n  2\n);"
        assert [method for method, _params in adapter.client.requests] == [
            "textDocument/documentSymbol",
            "textDocument/selectionRange",
        ]
    finally:
        runtime.stop()


def test_typescript_destructured_body_fails_closed_without_selection_range_evidence(tmp_path: Path) -> None:
    source = tmp_path / "answer.ts"
    source.write_text("export const [tupleA, tupleB] = [1, 2];\n")
    identifier = {"start": {"line": 0, "character": 14}, "end": {"line": 0, "character": 20}}
    runtime, _adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": [
                {"name": "tupleA", "kind": 14, "range": identifier, "selectionRange": identifier}
            ],
            "textDocument/selectionRange": None,
        },
    )
    try:
        ordinary = runtime.find_symbol("tupleA", relative_path="answer.ts").to_dict()
        body = runtime.find_symbol("tupleA", relative_path="answer.ts", include_body=True).to_dict()

        assert ordinary["ok"] is True
        assert body["error"]["code"] == "UNSUPPORTED"
        assert body["error"]["details"]["reason"] == "incomplete_assignment_range"
        assert body["error"]["details"]["recovery_reason"] == "selection_range_unavailable"
    finally:
        runtime.stop()


def test_typescript_assignment_name_mismatch_cannot_supply_body_or_edit_target(tmp_path: Path) -> None:
    """A server-reported symbol named ``good`` but positioned at the
    ``other`` identifier is exactly the anchor-mismatch a compromised or
    buggy server response could produce. Even though the supplied syntax
    ancestry would otherwise recover a complete statement, the production
    path must never read or edit ``other``'s statement under the ``good``
    name: it fails typed for ``include_body`` and finds no editable target,
    leaving the file byte-for-byte unchanged with no replacement notice."""

    source = tmp_path / "wrong_anchor.ts"
    original = b"export const good = 1;\nexport const other = 2;\n"
    source.write_bytes(original)
    wrong_anchor = {
        "start": {"line": 1, "character": 13},
        "end": {"line": 1, "character": 18},
    }
    other_through_value = {
        "start": {"line": 1, "character": 13},
        "end": {"line": 1, "character": 22},
    }
    statement = {
        "start": {"line": 1, "character": 0},
        "end": {"line": 1, "character": 23},
    }
    raw_symbol = {
        "name": "good",
        "kind": 14,
        "range": other_through_value,
        "selectionRange": wrong_anchor,
        "children": [],
    }
    runtime, adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": [raw_symbol],
            "textDocument/selectionRange": [{"range": wrong_anchor, "parent": {"range": statement}}],
        },
    )
    try:
        body = runtime.find_symbol("good", relative_path="wrong_anchor.ts", include_body=True).to_dict()
        edit = runtime.replace_symbol_body(
            "good",
            "wrong_anchor.ts",
            "good = 3",
            hashlib.sha256(original).hexdigest(),
        ).to_dict()

        assert body["error"]["code"] == "UNSUPPORTED"
        assert body["error"]["details"]["reason"] == "incomplete_assignment_range"
        assert body["error"]["details"]["recovery_reason"] == "no_enclosing_assignment"
        assert edit["error"]["code"] == "SYMBOL_NOT_FOUND"
        assert source.read_bytes() == original
        assert adapter.client.notifications == []
    finally:
        runtime.stop()


def test_directory_find_symbol_is_bounded_by_inventory_prefix_without_workspace_walk(tmp_path: Path) -> None:
    first = tmp_path / "src/a.py"
    second = tmp_path / "src/nested/b.py"
    sibling = tmp_path / "sibling/c.py"
    first.parent.mkdir()
    second.parent.mkdir(parents=True)
    sibling.parent.mkdir()
    for path in (first, second, sibling):
        path.write_text("class Target: pass\n")
    runtime, adapter, _policy = _runtime(
        tmp_path,
        {"textDocument/documentSymbol": [_symbol("Target")]},
    )
    try:
        result = runtime.find_symbol("Target", relative_path="src", include_body=True).to_dict()

        assert result["ok"] is True
        assert [item["relative_path"] for item in result["data"]["symbols"]] == [
            "src/a.py",
            "src/nested/b.py",
        ]
        requests = [params for method, params in adapter.client.requests if method == "textDocument/documentSymbol"]
        assert len(requests) == 2
        assert all("sibling/c.py" not in str(params) for params in requests)
        unsupported = runtime.find_symbol(
            "Target",
            relative_path="src",
            max_candidates_per_adapter=4,
        ).to_dict()
        assert unsupported["error"]["code"] == "UNSUPPORTED"
        document_requests = [
            method for method, _params in adapter.client.requests if method == "textDocument/documentSymbol"
        ]
        assert len(document_requests) == 2
    finally:
        runtime.stop()


def test_diagnostics_distinguish_not_ready_and_stale_timeout(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("x = 1\n")
    cold_runtime, _cold, _cold_policy = _runtime(tmp_path, {"textDocument/documentSymbol": []}, phase=AdapterPhase.COLD)
    try:
        cold = cold_runtime.get_diagnostics_for_file("main.py", timeout_seconds=0.01).to_dict()
        assert cold["error"]["code"] == "NOT_READY"
    finally:
        cold_runtime.stop()

    stale_runtime, stale_adapter, _stale_policy = _runtime(tmp_path, {"textDocument/documentSymbol": []})
    try:
        stale_adapter.diagnostics = DiagnosticsSnapshot(DiagnosticsState.STALE, source.as_uri(), source, 1, 1, 1, ())
        stale = stale_runtime.get_diagnostics_for_file("main.py", timeout_seconds=0.01).to_dict()
        assert stale["error"]["code"] == "TIMED_OUT"
        assert stale["error"]["details"]["publication_state"] == "stale"
    finally:
        stale_runtime.stop()

    raced_runtime, raced_adapter, _raced_policy = _runtime(tmp_path, {"textDocument/documentSymbol": []})
    try:
        raced_adapter.diagnostics = DiagnosticsSnapshot(
            DiagnosticsState.STALE,
            source.as_uri(),
            source,
            1,
            1,
            1,
            (),
        )

        def publish_while_cancelling(target: DocumentReadinessTarget) -> None:
            raced_adapter.diagnostics = DiagnosticsSnapshot(
                DiagnosticsState.CLEAN,
                target.uri,
                target.absolute_path,
                target.version,
                target.document_generation,
                2,
                (),
            )

        raced_adapter.cancel_diagnostics_target = publish_while_cancelling  # type: ignore[attr-defined]
        won_race = raced_runtime.get_diagnostics_for_file("main.py", timeout_seconds=0.01).to_dict()

        assert won_race["ok"] is True
        assert won_race["data"]["state"] == "clean"
        assert won_race["data"]["diagnostics_generation"] == 2
    finally:
        raced_runtime.stop()


def _count_scans(runtime: WorkspaceRuntime) -> list[int]:
    """Count guarded scans: exactly one inventory rebuild happens per scan."""

    counter = [0]
    real_rebuild = runtime.rebuild_inventory

    def counting_rebuild() -> TrustInventory:
        counter[0] += 1
        return real_rebuild()

    runtime.rebuild_inventory = counting_rebuild  # type: ignore[method-assign]
    return counter


def _race_document_loads(adapter: _Adapter, race: Callable[[int], None]) -> list[int]:
    """Complete a foreign write once each operation has captured its bytes."""

    loads = [0]
    real_load = adapter.snapshot_open_and_probe_document

    def racing_load(**kwargs: Any) -> Future[Any]:
        loads[0] += 1
        future = real_load(**kwargs)
        observed = future.result(timeout=5)
        race(loads[0])
        settled: Future[Any] = Future()
        settled.set_result(observed)
        return settled

    adapter.snapshot_open_and_probe_document = racing_load  # type: ignore[method-assign]
    return loads


def _publish_clean(adapter: _Adapter) -> None:
    def publish_while_cancelling(target: DocumentReadinessTarget) -> None:
        adapter.diagnostics = DiagnosticsSnapshot(
            DiagnosticsState.CLEAN,
            target.uri,
            target.absolute_path,
            target.version,
            target.document_generation,
            2,
            (),
        )

    adapter.cancel_diagnostics_target = publish_while_cancelling  # type: ignore[attr-defined]


def test_raced_diagnostic_clean_replays_once_and_returns_the_settled_clean_state(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("x = 1\n")
    runtime, adapter, _policy = _runtime(tmp_path, {"textDocument/documentSymbol": []})
    _publish_clean(adapter)
    scans = _count_scans(runtime)
    # The fixed inventory of this fixture never gains members, so the race
    # rewrites the tracked source that the read itself depends on.
    loads = _race_document_loads(
        adapter,
        lambda attempt: (tmp_path / "main.py").write_text("x = 2\n") if attempt == 1 else None,
    )
    try:
        result = runtime.get_diagnostics_for_file("main.py", timeout_seconds=0.01).to_dict()

        # A diagnostic `clean` state is source-derived success and takes the
        # same postflight and replay as navigation content.
        assert result["ok"] is True
        assert result["data"]["state"] == "clean"
        assert scans[0] == 4
        assert loads[0] == 2
    finally:
        runtime.stop()


def test_diagnostic_clean_raced_twice_returns_not_ready_and_never_reports_clean(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("x = 1\n")
    runtime, adapter, _policy = _runtime(tmp_path, {"textDocument/documentSymbol": []})
    _publish_clean(adapter)
    scans = _count_scans(runtime)
    loads = _race_document_loads(
        adapter,
        lambda attempt: (tmp_path / "main.py").write_text(f"x = {attempt + 1}\n"),
    )
    try:
        result = runtime.get_diagnostics_for_file("main.py", timeout_seconds=0.01).to_dict()

        assert result["ok"] is False
        assert "data" not in result
        assert result["error"]["code"] == "NOT_READY"
        assert result["error"]["retry"]["retryable"] is True
        assert result["error"]["details"]["reason"] == "workspace_changed_during_read"
        assert result["error"]["details"]["attempts"] == 2
        assert scans[0] == 4
        assert loads[0] == 2
    finally:
        runtime.stop()


def test_typed_trust_failure_is_returned_once_without_a_postflight_or_replay(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("x = 1\n")
    runtime, adapter, policy = _runtime(tmp_path, {"textDocument/documentSymbol": []})
    scans = _count_scans(runtime)
    try:
        policy.failure = WorkspaceError(WorkspaceErrorData(WorkspaceErrorCode.OUT_OF_WORKSPACE, "blocked", path=source))

        result = runtime.get_symbols_overview("main.py").to_dict()

        assert result["error"]["code"] == "OUT_OF_WORKSPACE"
        # The typed failure keeps its own authority: one preflight, no
        # postflight, and no replay that could manufacture another error.
        assert scans[0] == 1
        assert adapter.document_loads == []
    finally:
        runtime.stop()


def test_replace_symbol_body_takes_exactly_one_scan_and_one_dispatch(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    original = b"def target():\n    return 1\n"
    source.write_bytes(original)
    runtime, adapter, _policy = _runtime(tmp_path, {"textDocument/documentSymbol": [_symbol("target")]})
    scans = _count_scans(runtime)
    try:
        result = runtime.replace_symbol_body(
            "target", "main.py", "def target():\n    return 2", hashlib.sha256(original).hexdigest()
        ).to_dict()

        assert result["ok"] is True
        # Edits keep exactly one preflight and stay outside the read replay
        # boundary: no postflight scan and no second dispatch.
        assert scans[0] == 1
        assert adapter.edit_dispatches == 1
    finally:
        runtime.stop()


def test_replace_symbol_body_uses_one_edit_dispatch_and_notifies_after_install(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    original = b"def target():\n    return 1\n"
    source.write_bytes(original)
    symbols = [
        {
            "name": "target",
            "kind": 12,
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 1, "character": 12}},
            "selectionRange": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 10}},
        }
    ]
    runtime, adapter, policy = _runtime(tmp_path, {"textDocument/documentSymbol": symbols})
    try:
        result = runtime.replace_symbol_body(
            "target", "main.py", "def target():\n    return 2", hashlib.sha256(original).hexdigest()
        ).to_dict()
        assert result["ok"] is True
        assert adapter.edit_dispatches == 1
        # Authorized once before dispatch and re-walked under the workspace lock.
        assert policy.edit_calls == [source, source]
        assert source.read_text() == "def target():\n    return 2\n"
        assert [method for method, _ in adapter.client.notifications] == ["textDocument/didChange"]
        assert adapter.client.requests == [("textDocument/documentSymbol", {"textDocument": {"uri": source.as_uri()}})]
        assert result["adapter"] == {"name": "pyright", "language": "python"}
        assert result["generations"]["document"] == 2
        status = cast(Mapping[str, Mapping[str, Mapping[str, Mapping[str, int]]]], runtime.status())
        assert status["adapters"]["python"]["generations"]["document"] == 2
    finally:
        runtime.stop()


def test_replace_symbol_body_stale_hash_does_not_request_lsp(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("def target():\n    return 1\n")
    runtime, adapter, _policy = _runtime(tmp_path, {"textDocument/documentSymbol": [_symbol("target")]})
    try:
        result = runtime.replace_symbol_body("target", "main.py", "x", "0" * 64).to_dict()
        assert result["error"]["code"] == "STALE_HASH"
        assert adapter.edit_dispatches == 1
        assert adapter.client.requests == []
        assert adapter.client.notifications == []
    finally:
        runtime.stop()


def test_replace_symbol_body_authorizes_before_adapter_dispatch(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("def target():\n    return 1\n")
    runtime, adapter, policy = _runtime(tmp_path, {"textDocument/documentSymbol": [_symbol("target")]})
    policy.failure = WorkspaceError(WorkspaceErrorData(WorkspaceErrorCode.READ_ONLY_ROOT, "read only", path=source))
    try:
        result = runtime.replace_symbol_body("target", "main.py", "x", hashlib.sha256(source.read_bytes()).hexdigest())
        assert result.to_dict()["error"]["code"] == "READ_ONLY_ROOT"
        assert adapter.edit_dispatches == 0
        assert adapter.client.requests == []
    finally:
        runtime.stop()


def test_replace_symbol_body_typescript_plain_multiline_statement_replaces_through_semicolon(
    tmp_path: Path,
) -> None:
    """Task: replacing a recovered plain ``export const`` statement (whose
    server range omits declaration syntax) must replace the complete
    original statement including its terminal semicolon exactly once, never
    leaving a duplicated ``export const`` behind."""

    source = tmp_path / "multiline.ts"
    original = b"export const multiline = (\n  1 +\n  2\n);\n"
    source.write_bytes(original)
    selection = {"start": {"line": 0, "character": 13}, "end": {"line": 0, "character": 22}}
    server_range = {"start": {"line": 0, "character": 13}, "end": {"line": 3, "character": 1}}
    statement = {"start": {"line": 0, "character": 0}, "end": {"line": 3, "character": 2}}
    runtime, adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": [
                {"name": "multiline", "kind": 14, "range": server_range, "selectionRange": selection}
            ],
            "textDocument/selectionRange": [{"range": selection, "parent": {"range": statement}}],
        },
    )
    try:
        result = runtime.replace_symbol_body(
            "multiline",
            "multiline.ts",
            "export const multiline = 100;",
            hashlib.sha256(original).hexdigest(),
        ).to_dict()

        assert result["ok"] is True
        assert source.read_text() == "export const multiline = 100;\n"
        assert source.read_text().count("export const") == 1
        assert adapter.edit_dispatches == 1
    finally:
        runtime.stop()


def test_replace_symbol_body_typescript_destructured_binding_replaces_complete_statement(tmp_path: Path) -> None:
    """Task: replacing a recovered destructured top-level binding must
    replace the complete variable statement (through its terminal
    semicolon), never leaving a duplicated ``export const`` behind."""

    source = tmp_path / "answer.ts"
    original = b"export const [tupleA, tupleB] = [1, 2];\n"
    source.write_bytes(original)
    first = {"start": {"line": 0, "character": 14}, "end": {"line": 0, "character": 20}}
    second = {"start": {"line": 0, "character": 22}, "end": {"line": 0, "character": 28}}
    binding = {"start": {"line": 0, "character": 13}, "end": {"line": 0, "character": 29}}
    statement = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 39}}
    symbols = [
        {"name": "tupleA", "kind": 14, "range": first, "selectionRange": first},
        {"name": "tupleB", "kind": 14, "range": second, "selectionRange": second},
    ]
    selection_ranges = [
        {"range": first, "parent": {"range": binding, "parent": {"range": statement}}},
        {"range": second, "parent": {"range": binding, "parent": {"range": statement}}},
    ]
    runtime, adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": symbols,
            "textDocument/selectionRange": selection_ranges,
        },
    )
    try:
        result = runtime.replace_symbol_body(
            "tupleA",
            "answer.ts",
            "export const [tupleA, tupleB] = [9, 9];",
            hashlib.sha256(original).hexdigest(),
        ).to_dict()

        assert result["ok"] is True
        assert source.read_text() == "export const [tupleA, tupleB] = [9, 9];\n"
        assert source.read_text().count("export const") == 1
        assert adapter.edit_dispatches == 1
    finally:
        runtime.stop()


def test_replace_symbol_body_typescript_unavailable_selection_range_fails_closed_without_mutation(
    tmp_path: Path,
) -> None:
    """Task: malformed/unavailable selection-range ancestry for a
    destructured binding must fail typed without ever writing the file,
    exactly like the read-only ``include_body`` contract."""

    source = tmp_path / "answer.ts"
    original = b"export const [tupleA, tupleB] = [1, 2];\n"
    source.write_bytes(original)
    identifier = {"start": {"line": 0, "character": 14}, "end": {"line": 0, "character": 20}}
    runtime, _adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": [
                {"name": "tupleA", "kind": 14, "range": identifier, "selectionRange": identifier}
            ],
            "textDocument/selectionRange": None,
        },
    )
    try:
        result = runtime.replace_symbol_body(
            "tupleA",
            "answer.ts",
            "export const [tupleA, tupleB] = [9, 9];",
            hashlib.sha256(original).hexdigest(),
        ).to_dict()

        assert result["ok"] is False
        assert result["error"]["code"] in {"SYMBOL_NOT_FOUND", "UNSUPPORTED"}
        assert source.read_bytes() == original
    finally:
        runtime.stop()


def test_replace_symbol_body_typescript_stale_hash_does_not_mutate(tmp_path: Path) -> None:
    source = tmp_path / "answer.ts"
    original = b"export const answer = 1;\n"
    source.write_bytes(original)
    identifier = {"start": {"line": 0, "character": 13}, "end": {"line": 0, "character": 19}}
    runtime, adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": [
                {"name": "answer", "kind": 14, "range": identifier, "selectionRange": identifier}
            ],
        },
    )
    try:
        result = runtime.replace_symbol_body("answer", "answer.ts", "export const answer = 2;", "0" * 64).to_dict()

        assert result["error"]["code"] == "STALE_HASH"
        assert source.read_bytes() == original
        assert adapter.client.notifications == []
    finally:
        runtime.stop()


def test_global_find_symbol_warms_from_one_exact_candidate(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("class Target: pass\n")
    (tmp_path / "unrelated.py").write_text("class Other: pass\n")
    runtime, adapter, _policy = _runtime(
        tmp_path,
        {
            "workspace/symbol": [
                {
                    "name": "Target",
                    "kind": 12,
                    "location": {"uri": source.as_uri(), "range": _symbol("Target")["range"]},
                }
            ],
            "textDocument/documentSymbol": [_symbol("Target")],
        },
        phase=AdapterPhase.COLD,
    )
    try:
        result = runtime.find_symbol("Target").to_dict()
        assert result["ok"] is True
        assert adapter.phase is AdapterPhase.READY
        assert adapter.document_loads == ["main.py"]
        assert "unrelated.py" not in adapter.document_loads
        assert [method for method, _ in adapter.client.requests] == [
            "textDocument/documentSymbol",
            "workspace/symbol",
        ]
    finally:
        runtime.stop()


def test_global_find_symbol_waits_for_delayed_exact_candidate_without_walking_files(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("class Target: pass\n")
    (tmp_path / "unrelated.py").write_text("class Other: pass\n")
    polls = 0

    def delayed_candidate() -> list[dict[str, object]]:
        nonlocal polls
        polls += 1
        if polls == 1:
            return []
        return [
            {
                "name": "Target",
                "kind": 12,
                "location": {"uri": source.as_uri(), "range": _symbol("Target")["range"]},
            }
        ]

    runtime, adapter, _policy = _runtime(
        tmp_path,
        {
            "workspace/symbol": delayed_candidate,
            "textDocument/documentSymbol": [_symbol("Target")],
        },
        phase=AdapterPhase.STARTING,
        future_timeout=0.2,
    )
    try:
        result = runtime.find_symbol("Target").to_dict()

        assert result["ok"] is True
        assert adapter.phase is AdapterPhase.READY
        assert polls >= 2  # discovery miss, then the controlled-document sentinel
        assert adapter.document_loads == ["main.py"]
        assert "unrelated.py" not in adapter.document_loads
        assert all(
            method in {"workspace/symbol", "textDocument/documentSymbol"} for method, _params in adapter.client.requests
        )
    finally:
        runtime.stop()


def test_global_find_symbol_waits_to_budget_then_returns_not_ready_without_document_probe(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("class Target: pass\n")
    runtime, adapter, _policy = _runtime(
        tmp_path,
        {"workspace/symbol": []},
        phase=AdapterPhase.STARTING,
        future_timeout=0.05,
    )
    try:
        started = time.monotonic()
        result = runtime.find_symbol("Target").to_dict()
        elapsed = time.monotonic() - started

        assert result["error"]["code"] == "NOT_READY"
        assert elapsed >= 0.04
        assert elapsed < 0.3
        assert adapter.phase is AdapterPhase.STARTING
        assert adapter.document_loads == ["main.py"]
        assert adapter.client.requests
        assert {method for method, _params in adapter.client.requests} == {"textDocument/documentSymbol"}
    finally:
        runtime.stop()


def test_global_warm_round_robin_does_not_starve_later_family(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("class Other: pass\n")
    source = tmp_path / "main.ts"
    source.write_text("class Target {}\n")
    polls = 0

    def first_family_stays_empty() -> list[dict[str, object]]:
        nonlocal polls
        polls += 1
        if polls == 1:
            return []
        return [
            {
                "name": "Target",
                "kind": 12,
                "location": {"uri": source.as_uri(), "range": _symbol("Target")["range"]},
            }
        ]

    runtime, python_adapter, _policy = _runtime(
        tmp_path,
        {
            "workspace/symbol": first_family_stays_empty,
            "textDocument/documentSymbol": [_symbol("Target")],
        },
        phase=AdapterPhase.STARTING,
        future_timeout=0.2,
    )
    try:
        result = runtime.find_symbol("Target").to_dict()
        status = runtime.status()
        adapters = cast(Mapping[str, Mapping[str, object]], status["adapters"])

        assert result["error"]["code"] == "NOT_READY"
        assert python_adapter.phase is AdapterPhase.STARTING
        assert python_adapter.document_loads == ["main.py"]
        assert adapters["typescript"]["phase"] == "ready"
        assert cast(Mapping[str, int], adapters["typescript"]["generations"])["document"] == 1
    finally:
        runtime.stop()


def test_public_tools_map_policy_and_routing_failures_to_envelopes(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("x = 1\n")
    runtime, adapter, policy = _runtime(tmp_path, {"textDocument/documentSymbol": []})
    try:
        policy.failure = WorkspaceError(WorkspaceErrorData(WorkspaceErrorCode.OUT_OF_WORKSPACE, "blocked", path=source))
        assert runtime.get_symbols_overview("main.py").to_dict()["error"]["code"] == "OUT_OF_WORKSPACE"
        policy.failure = None
        adapter.routes = lambda _path: False  # type: ignore[method-assign]
        assert runtime.get_symbols_overview("main.py").to_dict()["error"]["code"] == "UNSUPPORTED"
    finally:
        runtime.stop()


def _configured_python_projection(trusted: tuple[str, ...], program: tuple[str, ...]) -> ScopeProjection:
    return ScopeProjection.from_attribution(
        trust_inventory_paths=trusted,
        attribution=NativeProgramAttribution(
            LanguageFamily.PYTHON,
            ProjectKind.CONFIGURED,
            "pyrightconfig.json",
            program,
        ),
    )


def test_references_report_native_exclusions_with_one_bounded_coverage_object(tmp_path: Path) -> None:
    source = tmp_path / "src/source.py"
    source.parent.mkdir()
    source.write_text("target()\n")
    for index in range(20):
        excluded = tmp_path / "tests" / f"excluded_{index:02d}.py"
        excluded.parent.mkdir(exist_ok=True)
        excluded.write_text("target()\n")
    trusted = tuple(sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*.py")))
    projection = _configured_python_projection(trusted, ("src/source.py",))
    runtime, _adapter, _policy = _runtime(
        tmp_path,
        {"textDocument/documentSymbol": [_symbol("target")], "textDocument/references": []},
        attributors={LanguageFamily.PYTHON: lambda _root, _paths: projection},
    )
    try:
        result = runtime.find_referencing_symbols("src/source.py", "target").to_dict()

        assert result["ok"] is True
        coverage = result["data"]["coverage"]
        assert coverage["adapter"] == "pyright"
        assert coverage["language"] == "python"
        assert coverage["scope_kind"] == "configured"
        assert coverage["configured_program_files"] == 1
        assert coverage["configured_program_digest"] == projection.configured_program.sha256
        assert coverage["trusted_language_files"] == 21
        assert coverage["trusted_language_digest"] == projection.trust_inventory.sha256
        assert coverage["uncovered_files"] == 20
        assert coverage["uncovered_sample"]["total"] == 20
        assert coverage["uncovered_sample"]["items"] == [f"tests/excluded_{index:02d}.py" for index in range(16)]
        assert coverage["uncovered_sample"]["omitted"] == 4
        assert len(coverage["uncovered_sample"]["digest"]) == 64
        assert result["data"]["references"] == []
    finally:
        runtime.stop()


def test_references_exclude_open_workspace_files_outside_configured_program(tmp_path: Path) -> None:
    source = tmp_path / "src/source.py"
    source.parent.mkdir()
    source.write_text("target()\n")
    excluded = tmp_path / "tests/excluded.py"
    excluded.parent.mkdir()
    excluded.write_text("target()\n")
    projection = _configured_python_projection(
        ("src/source.py", "tests/excluded.py"),
        ("src/source.py",),
    )
    references = [
        {
            "uri": path.as_uri(),
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 6}},
        }
        for path in (source, excluded)
    ]
    runtime, adapter, _policy = _runtime(
        tmp_path,
        {"textDocument/documentSymbol": [_symbol("target")], "textDocument/references": references},
        attributors={LanguageFamily.PYTHON: lambda _root, _paths: projection},
    )
    try:
        result = runtime.find_referencing_symbols("src/source.py", "target").to_dict()

        assert result["ok"] is True
        assert [reference["path"] for reference in result["data"]["references"]] == ["src/source.py"]
        assert result["data"]["coverage"]["uncovered_sample"]["items"] == ["tests/excluded.py"]
        assert "tests/excluded.py" not in adapter.document_loads
    finally:
        runtime.stop()


def _external_files(root: Path, count: int) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(count):
        path = root / f"ext_{index:03d}.py"
        path.write_text("target()\n")
        paths.append(path.resolve())
    return paths


def test_references_target_cap_applies_to_full_unique_set_all_external(tmp_path: Path) -> None:
    """Task: MAX_RESPONSE_OWNED_TARGETS bounds the complete unique target set
    before internal/external partitioning; an all-external overflow must
    fail deterministically rather than retry or silently truncate."""

    source = tmp_path / "src/source.py"
    source.parent.mkdir()
    source.write_text("target()\n")
    external_paths = _external_files(tmp_path.parent / f"{tmp_path.name}-external-a", 65)
    references = [
        {"uri": path.as_uri(), "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 6}}}
        for path in external_paths
    ]
    runtime, adapter, policy = _runtime(
        tmp_path,
        {"textDocument/documentSymbol": [_symbol("target")], "textDocument/references": references},
    )
    policy.read_only_external_paths = set(external_paths)
    try:
        result = runtime.find_referencing_symbols("src/source.py", "target").to_dict()

        assert result["error"]["code"] == "UNSUPPORTED"
        assert result["error"].get("retry") is None
        details = result["error"]["details"]
        assert details["reason"] == "semantic target set exceeds snapshot bound"
        assert details["total"] == 65
        assert details["omitted"] == 1
        assert len(details["paths"]) == 64
        assert details["paths"] == sorted(details["paths"])
    finally:
        runtime.stop()


def test_references_target_cap_applies_to_full_unique_set_mixed_internal_and_external(tmp_path: Path) -> None:
    """A mix of internal and external targets must be bounded by their
    combined unique count, and no internal target may be opened or read
    before the oversize rejection is returned."""

    source = tmp_path / "src/source.py"
    source.parent.mkdir()
    source.write_text("target()\n")
    internal_paths = []
    for index in range(40):
        internal = tmp_path / "src" / f"internal_{index:03d}.py"
        internal.write_text("target()\n")
        internal_paths.append(internal)
    external_paths = _external_files(tmp_path.parent / f"{tmp_path.name}-external-b", 30)
    references = [
        {"uri": path.as_uri(), "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 6}}}
        for path in (*internal_paths, *external_paths)
    ]
    runtime, adapter, policy = _runtime(
        tmp_path,
        {"textDocument/documentSymbol": [_symbol("target")], "textDocument/references": references},
    )
    policy.read_only_external_paths = set(external_paths)
    try:
        result = runtime.find_referencing_symbols("src/source.py", "target").to_dict()

        assert result["error"]["code"] == "UNSUPPORTED"
        assert result["error"].get("retry") is None
        details = result["error"]["details"]
        assert details["reason"] == "semantic target set exceeds snapshot bound"
        assert details["total"] == 70
        assert details["omitted"] == 6
        assert len(details["paths"]) == 64
        # No internal target's snapshot may be opened or read before the
        # complete unique set is confirmed within bound; only the unrelated
        # source document (loaded to resolve the query symbol) may appear.
        assert adapter.document_loads == ["src/source.py"]
    finally:
        runtime.stop()


def test_declaration_target_cap_allows_exactly_the_boundary_count(tmp_path: Path) -> None:
    """Exactly 64 unique targets remains allowed; the bound is exclusive.

    ``find_declaration`` is used here (rather than references) because a
    purely external target set can render successfully through the raw-LSP
    basis without a reference snippet, isolating the count boundary itself.
    """

    source = tmp_path / "src/main.py"
    source.parent.mkdir()
    source.write_text("target()\n")
    external_paths = _external_files(tmp_path.parent / f"{tmp_path.name}-external-c", 64)
    definitions = [
        {"uri": path.as_uri(), "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 6}}}
        for path in external_paths
    ]
    runtime, _adapter, policy = _runtime(
        tmp_path,
        {"textDocument/documentSymbol": [_symbol("target")], "textDocument/definition": definitions},
    )
    policy.read_only_external_paths = set(external_paths)
    try:
        result = runtime.find_declaration("src/main.py", r"(target)\(\)").to_dict()

        assert result["ok"] is True
        assert len(result["data"]["locations"]) == 64
    finally:
        runtime.stop()


def test_reference_target_change_between_response_and_snapshot_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "src/source.py"
    source.parent.mkdir()
    source.write_text("target()\n")
    target = tmp_path / "src/target.py"
    target.write_text("def target(): pass\n")
    calls = 0

    def changing_references() -> list[Mapping[str, object]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            target.write_text("# moved\ndef target(): pass\n")
            line = 0
        else:
            line = 1
        return [
            {
                "uri": target.as_uri(),
                "range": {
                    "start": {"line": line, "character": 4},
                    "end": {"line": line, "character": 10},
                },
            }
        ]

    runtime, _adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": [_symbol("target")],
            "textDocument/references": changing_references,
        },
    )
    try:
        result = runtime.find_referencing_symbols("src/source.py", "target").to_dict()

        assert result["error"]["code"] == "NOT_READY"
        assert result["error"]["retry"]["retryable"] is True
        assert result["error"]["details"]["reason"] == "semantic target locations changed"
        assert calls == 2
    finally:
        runtime.stop()


def test_external_reference_without_server_snapshot_binding_renders_raw_only(tmp_path: Path) -> None:
    source = tmp_path / "src/source.py"
    source.parent.mkdir()
    source.write_text("target()\n")
    external = tmp_path.parent / "site-packages" / "pkg.py"
    external.parent.mkdir(parents=True, exist_ok=True)
    external.write_text("def target(): pass\n")
    location = {
        "uri": external.as_uri(),
        "range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 10}},
    }
    runtime, adapter, policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": [_symbol("target")],
            "textDocument/references": [location],
        },
    )
    adapter.position_encoding = PositionEncoding.UTF32
    policy.read_only_external_paths.add(external.resolve())
    try:
        result = runtime.find_referencing_symbols("src/source.py", "target").to_dict()

        assert result["ok"] is True
        assert result["data"]["reference_count"] == 1
        assert result["data"]["coverage"]["adapter"] == "pyright"
        assert result["data"]["references"] == [
            {
                "path": str(external.resolve()),
                "read_only_external": True,
                "location": {
                    "basis": "lsp_zero_based_line_unicode_code_point_character",
                    "start": {"line": 0, "character": 4},
                    "end": {"line": 0, "character": 10},
                },
                "container": {"kind": "file", "name_path": "<file>"},
            }
        ]
        assert adapter.document_loads == ["src/source.py"]
    finally:
        runtime.stop()


def test_external_reference_stale_response_generation_remains_not_ready(tmp_path: Path) -> None:
    source = tmp_path / "src/source.py"
    source.parent.mkdir()
    source.write_text("target()\n")
    external = tmp_path.parent / "site-packages" / "stale.py"
    external.parent.mkdir(parents=True, exist_ok=True)
    external.write_text("def target(): pass\n")
    location = {
        "uri": external.as_uri(),
        "range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 10}},
    }
    runtime, adapter, policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": [_symbol("target")],
            "textDocument/references": [location],
        },
    )
    policy.read_only_external_paths.add(external.resolve())
    original_classify = runtime.classify_reference_location
    transitioned = False

    def classify_after_transition(value: Location) -> ReferenceTarget | ErrorEnvelope:
        nonlocal transitioned
        classified = original_classify(value)
        if not transitioned:
            transitioned = True
            adapter.document_generation += 1
        return classified

    runtime.classify_reference_location = classify_after_transition  # type: ignore[method-assign]
    try:
        result = runtime.find_referencing_symbols("src/source.py", "target").to_dict()

        assert result["error"]["code"] == "NOT_READY"
        assert result["error"]["retry"]["retryable"] is True
        assert result["error"]["details"] == {
            "reason": "semantic target generation changed",
        }
    finally:
        runtime.stop()


def test_reference_cold_cooldown_and_capability_failures_are_not_empty_success(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("target()\n")
    replies = {"textDocument/documentSymbol": [_symbol("target")], "textDocument/references": []}
    runtime, adapter, _policy = _runtime(tmp_path, replies, phase=AdapterPhase.COLD)
    scans = _count_scans(runtime)
    try:
        assert runtime.find_referencing_symbols("source.py", "target").to_dict()["error"]["code"] == "NOT_READY"
        # A typed readiness failure keeps its own authority: one preflight per
        # call, no postflight, and no replay that could manufacture another
        # result.
        assert scans[0] == 1
        adapter.phase = AdapterPhase.COOLDOWN
        assert runtime.find_referencing_symbols("source.py", "target").to_dict()["error"]["code"] == "COOLDOWN"
        assert scans[0] == 2
        assert [method for method, _ in adapter.client.requests if method == "textDocument/references"] == []
    finally:
        runtime.stop()

    runtime, adapter, _policy = _runtime(
        tmp_path,
        replies,
        raw_providers=RawLspProviders(document_symbols=True, references=False),
    )
    scans = _count_scans(runtime)
    try:
        assert runtime.find_referencing_symbols("source.py", "target").to_dict()["error"]["code"] == "UNSUPPORTED"
        assert scans[0] == 1
        assert [method for method, _ in adapter.client.requests if method == "textDocument/references"] == []
    finally:
        runtime.stop()


def test_reference_timeout_remains_typed_failure(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("target()\n")
    started = threading.Event()
    release = threading.Event()

    def blocking_references() -> list[object]:
        started.set()
        assert release.wait(5)
        return []

    runtime, _adapter, _policy = _runtime(
        tmp_path,
        {"textDocument/documentSymbol": [_symbol("target")], "textDocument/references": blocking_references},
        future_timeout=0.05,
    )
    try:
        result = runtime.find_referencing_symbols("source.py", "target").to_dict()
        assert started.is_set()
        assert result["error"]["code"] == "TIMED_OUT"
        assert result["error"]["retry"]["retryable"] is True
    finally:
        release.set()
        runtime.stop()


def test_incompatible_python_does_not_block_healthy_typescript_references(tmp_path: Path) -> None:
    python = tmp_path / "broken.py"
    typescript = tmp_path / "healthy.ts"
    python.write_text("target()\n")
    typescript.write_text("target();\n")

    def incompatible_python(_root: Path, values: tuple[str, ...]) -> ScopeProjection:
        return ScopeProjection.from_attribution(
            trust_inventory_paths=values,
            attribution=NativeProgramAttribution(
                LanguageFamily.PYTHON,
                ProjectKind.CONFIGURED,
                "pyrightconfig.json",
                ("outside.py",),
            ),
        )

    runtime, _adapter, _policy = _runtime(
        tmp_path,
        {"textDocument/documentSymbol": [_symbol("target")], "textDocument/references": []},
        attributors={
            LanguageFamily.PYTHON: incompatible_python,
            LanguageFamily.TYPESCRIPT: lambda _root, values: _projection(LanguageFamily.TYPESCRIPT, values),
        },
    )
    try:
        failed = runtime.find_referencing_symbols("broken.py", "target").to_dict()
        healthy = runtime.find_referencing_symbols("healthy.ts", "target").to_dict()

        assert failed["error"]["code"] == "SCOPE_INCOMPATIBLE"
        assert healthy["ok"] is True
        assert healthy["data"]["coverage"]["adapter"] == "typescript"
        assert healthy["data"]["coverage"]["language"] == "typescript"
        assert healthy["data"]["coverage"]["configured_program_files"] == 1
    finally:
        runtime.stop()


def _edit_symbols() -> list[Mapping[str, Any]]:
    return [
        {
            "name": "target",
            "kind": 12,
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 1, "character": 12}},
            "selectionRange": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 10}},
        }
    ]


def test_edit_that_times_out_while_queued_is_timed_out_and_can_never_write(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    original = b"def target():\n    return 1\n"
    source.write_bytes(original)
    runtime, adapter, _policy = _runtime(
        tmp_path, {"textDocument/documentSymbol": _edit_symbols()}, future_timeout=0.05
    )
    release = threading.Event()
    try:
        occupied = runtime.executor.submit(lambda: release.wait(5))
        result = runtime.replace_symbol_body(
            "target", "main.py", "def target():\n    return 2", hashlib.sha256(original).hexdigest()
        ).to_dict()

        assert result["error"]["code"] == "TIMED_OUT"
        assert result["error"]["details"]["commit_state"] == "queued"
        release.set()
        assert occupied.result(timeout=5) is True
        runtime.executor.submit(lambda: None).result(timeout=5)
        # A cancelled queued edit provably never reaches the filesystem.
        assert source.read_bytes() == original
        assert adapter.client.requests == []
    finally:
        release.set()
        runtime.stop()


def test_edit_that_times_out_while_running_is_uncertain_and_is_never_replayed(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    original = b"def target():\n    return 1\n"
    source.write_bytes(original)
    started = threading.Event()
    release = threading.Event()

    def blocking_document_symbol() -> object:
        started.set()
        assert release.wait(5)
        return _edit_symbols()

    runtime, _adapter, _policy = _runtime(
        tmp_path, {"textDocument/documentSymbol": blocking_document_symbol}, future_timeout=0.05
    )
    try:
        result = runtime.replace_symbol_body(
            "target", "main.py", "def target():\n    return 2", hashlib.sha256(original).hexdigest()
        ).to_dict()

        assert started.is_set()
        assert result["error"]["code"] == "UNCERTAIN"
        assert result["error"]["retry"] == {"retryable": False}
        assert result["error"]["details"]["commit_state"] == "running"
        assert result["error"]["details"]["uncertain_stage"] == "timeout"
        assert result["error"]["details"]["requires_current_reread"] is True
    finally:
        release.set()
        runtime.stop()


def test_lost_response_after_install_is_uncertain_with_the_observed_current_hash(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    original = b"def target():\n    return 1\n"
    source.write_bytes(original)
    runtime, adapter, _policy = _runtime(tmp_path, {"textDocument/documentSymbol": _edit_symbols()})

    def lose_response() -> None:
        raise ConnectionError("daemon connection closed after the edit")

    adapter.edit_epilogue = lose_response
    try:
        result = runtime.replace_symbol_body(
            "target", "main.py", "def target():\n    return 2", hashlib.sha256(original).hexdigest()
        ).to_dict()
        installed = source.read_bytes()

        assert installed != original
        assert result["error"]["code"] == "UNCERTAIN"
        assert result["error"]["details"]["commit_state"] == "done"
        assert result["error"]["details"]["uncertain_stage"] == "transport"
        assert result["error"]["details"]["current_hash"] == hashlib.sha256(installed).hexdigest()

        # The original expected hash must not be able to repeat the edit.
        adapter.edit_epilogue = None
        replay = runtime.replace_symbol_body(
            "target", "main.py", "def target():\n    return 2", hashlib.sha256(original).hexdigest()
        ).to_dict()
        assert replay["error"]["code"] == "STALE_HASH"
    finally:
        runtime.stop()
