from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from pathlib import Path, PurePosixPath
from typing import Any, cast

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
from serena_light.lsp.positions import FileSnapshot, PositionEncoding
from serena_light.lsp.state import DiagnosticsSnapshot, DiagnosticsState
from serena_light.tools.editing import NotificationResult, ReplacementNotification
from serena_light.tools.envelopes import GenerationMetadata
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

    def authorize_path_operand(
        self, identity: WorkspaceIdentity, path: str | Path, inventory: Sequence[Path]
    ) -> Path:
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
        self.diagnostics = DiagnosticsSnapshot(DiagnosticsState.MISSING, "", None, None, 0, None, ())

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
            PositionEncoding.UTF16,
            AdapterGenerations(1, 2, self.document_generation, 3),
            CrashSnapshot(0, 0, None, None, None, 0.0),
            (),
            True,
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
            self.document_loads.append(relative_path)
            self.document_generation += 1
            target = DocumentReadinessTarget(
                uri, relative_path, absolute_path, version, self.document_generation, 0
            )
            assert probe.observe(self.client, target, timeout=1.0)
            return FileSnapshot.from_bytes(absolute_path.read_bytes()), target

        return self.context.executor.submit(worker)

    def submit_read(self, operation: Callable[[_Client], Any]) -> Future[Any]:
        return self.context.executor.submit(lambda: operation(self.client))

    def submit_edit(self, operation: Callable[[_Client], Any]) -> Future[Any]:
        self.edit_dispatches += 1
        return self.context.executor.submit(lambda: operation(self.client))

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
        del client, text
        self.document_generation += 1
        self.document_loads.append(relative_path)
        return DocumentReadinessTarget(uri, relative_path, absolute_path, version, self.document_generation, 0)

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
) -> tuple[WorkspaceRuntime, _Adapter, _Policy]:
    paths = tuple(
        sorted(
            str(path.relative_to(tmp_path))
            for extension in ("*.py", "*.ts")
            for path in tmp_path.rglob(extension)
        )
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
        attributors={
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
    runtime, adapter, _policy = _runtime(
        tmp_path,
        {
            "textDocument/documentSymbol": [],
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
        ]
        assert result["data"]["capabilities"]["raw"]["declarationProvider"] is False
        location = result["data"]["locations"][0]
        assert location["body"] == "target"
        assert location["range"]["start"] == {"line": 2, "column": 7, "text_offset": 12, "byte_offset": 15}
        assert location["info"]["selection_range"] == location["range"]
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
        ]
        location = result["data"]["locations"][0]
        assert location["absolute_path"] == str(external.resolve())
        assert location["location_kind"] == "read_only_external"
        assert location["read_only_external"] is True
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
        ]
        location = result["data"]["locations"][0]
        assert location["relative_path"] == "src/implementation.ts"
        assert "kind" not in location
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


def test_diagnostics_distinguish_not_ready_and_stale_timeout(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("x = 1\n")
    cold_runtime, _cold, _cold_policy = _runtime(
        tmp_path, {"textDocument/documentSymbol": []}, phase=AdapterPhase.COLD
    )
    try:
        cold = cold_runtime.get_diagnostics_for_file("main.py", timeout_seconds=0.01).to_dict()
        assert cold["error"]["code"] == "NOT_READY"
    finally:
        cold_runtime.stop()

    stale_runtime, stale_adapter, _stale_policy = _runtime(tmp_path, {"textDocument/documentSymbol": []})
    try:
        stale_adapter.diagnostics = DiagnosticsSnapshot(
            DiagnosticsState.STALE, source.as_uri(), source, 1, 1, 1, ()
        )
        stale = stale_runtime.get_diagnostics_for_file("main.py", timeout_seconds=0.01).to_dict()
        assert stale["error"]["code"] == "TIMED_OUT"
        assert stale["error"]["details"]["publication_state"] == "stale"
    finally:
        stale_runtime.stop()


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
        assert policy.edit_calls == [source]
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
    policy.failure = WorkspaceError(
        WorkspaceErrorData(WorkspaceErrorCode.READ_ONLY_ROOT, "read only", path=source)
    )
    try:
        result = runtime.replace_symbol_body("target", "main.py", "x", hashlib.sha256(source.read_bytes()).hexdigest())
        assert result.to_dict()["error"]["code"] == "READ_ONLY_ROOT"
        assert adapter.edit_dispatches == 0
        assert adapter.client.requests == []
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
            method in {"workspace/symbol", "textDocument/documentSymbol"}
            for method, _params in adapter.client.requests
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
