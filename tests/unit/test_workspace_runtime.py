from __future__ import annotations

import hashlib
import threading
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
    RawLspProviders,
)
from serena_light.lsp.executor import BoundedLspExecutor
from serena_light.lsp.positions import FileSnapshot, PositionEncoding
from serena_light.tools.navigation import DocumentSymbolInput
from serena_light.workspace.identity import WorkspaceIdentity, WorkspaceKind
from serena_light.workspace.inventory import SupportedPathTree, TrustInventory
from serena_light.workspace.runtime import (
    AdapterBuildContext,
    AdapterFactory,
    RuntimeErrorCode,
    WorkspaceRuntime,
    WorkspaceRuntimeError,
)
from serena_light.workspace.scope import (
    LanguageFamily,
    NativeProgramAttribution,
    ProjectKind,
    ScopeProjection,
)


class _PathPolicy:
    def authorize_path_operand(
        self,
        identity: WorkspaceIdentity,
        path: str | Path,
        inventory: Sequence[Path],
    ) -> Path:
        candidate = Path(path).resolve(strict=True)
        if candidate not in {item.resolve(strict=True) for item in inventory}:
            raise ValueError(f"outside inventory: {candidate}")
        assert candidate.is_relative_to(identity.root)
        return candidate


class _Client:
    def __init__(self, symbols: Sequence[Mapping[str, Any]]) -> None:
        self.symbols = symbols
        self.requests: list[tuple[str, object, float | None]] = []

    def request(self, method: str, params: object = None, *, timeout: float | None = None) -> object:
        self.requests.append((method, params, timeout))
        return self.symbols

    def notify(self, method: str, params: object = None) -> None:
        del method, params

    def shutdown(self, *, timeout: float = 2.0) -> None:
        del timeout


class _Adapter:
    def __init__(
        self,
        context: AdapterBuildContext,
        *,
        symbols: Sequence[Mapping[str, Any]],
        on_snapshot: Callable[[], None] | None = None,
    ) -> None:
        self.context = context
        self.client = _Client(symbols)
        self.on_snapshot = on_snapshot
        self.open_versions: list[int] = []
        self.open_thread: str | None = None
        self.probe_thread: str | None = None
        self.stop_thread: str | None = None
        self._phase = AdapterPhase.COLD
        self._document_generation = 0
        self._running = False

    def routes(self, path: str | Path) -> bool:
        suffix = PurePosixPath(str(path)).suffix.lower()
        if self.context.family is LanguageFamily.PYTHON:
            return suffix in {".py", ".pyi"}
        return suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}

    def snapshot(self) -> AdapterSnapshot:
        scope = self.context.scope_tracker.generations
        raw = RawLspProviders(
            definition=True,
            implementation=self.context.family is LanguageFamily.TYPESCRIPT,
            references=True,
            document_symbols=True,
            workspace_symbols=True,
        )
        return AdapterSnapshot(
            name=self.context.family.value,
            phase=self._phase,
            raw_providers=raw,
            derived_tools=DerivedToolAvailability.from_raw(raw),
            engine=EngineMetadata(
                name=f"fake-{self.context.family.value}",
                version="1.0",
                executable=Path(f"/owned/{self.context.family.value}-server"),
                interpreter=Path("/owned/python") if self.context.family is LanguageFamily.PYTHON else None,
            ),
            position_encoding=PositionEncoding.UTF16,
            generations=AdapterGenerations(
                trust=scope.trust_inventory,
                program=scope.configured_program,
                document=self._document_generation,
                index=max(0, scope.observed_configured_program),
            ),
            crash=CrashSnapshot(
                total=0,
                window_count=0,
                last_timestamp=None,
                last_error=None,
                cooldown_until=None,
                cooldown_remaining_seconds=0.0,
            ),
            transitions=(),
            running=self._running,
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
        def run_atomic_operation() -> tuple[FileSnapshot, DocumentReadinessTarget]:
            self.open_thread = threading.current_thread().name
            self.open_versions.append(version)
            with self.context.operation_lock:
                snapshot = FileSnapshot.from_bytes(absolute_path.read_bytes())
                if self.on_snapshot is not None:
                    self.on_snapshot()
                self._document_generation += 1
                self._phase = AdapterPhase.DOCUMENT_READY
                self._running = True
                path_generation = self.context.scope_tracker.generations.path_scoped.get(relative_path, 0)
                target = DocumentReadinessTarget(
                    uri=uri,
                    relative_path=relative_path,
                    absolute_path=absolute_path,
                    version=version,
                    document_generation=self._document_generation,
                    path_generation=path_generation,
                )
                self.probe_thread = threading.current_thread().name
                if not probe.observe(self.client, target, timeout=1.0):
                    raise RuntimeError("document-symbol probe failed")
                assert self.context.scope_tracker.observe_path(relative_path, path_generation)
                return snapshot, target

        return self.context.executor.submit(run_atomic_operation)

    def stop(self) -> Future[AdapterSnapshot]:
        def stop_on_executor() -> AdapterSnapshot:
            self.stop_thread = threading.current_thread().name
            self._phase = AdapterPhase.STOPPING
            self._running = False
            return self.snapshot()

        return self.context.executor.submit(stop_on_executor)


def _inventory(root: Path, *paths: str) -> TrustInventory:
    ordered = tuple(sorted(paths))
    return TrustInventory(
        root=root.resolve(strict=True),
        paths=ordered,
        rejected=(),
        digest=hashlib.sha256("\0".join(ordered).encode()).hexdigest(),
        tree=SupportedPathTree.from_paths(ordered),
        kind="test",
    )


def _projection(
    family: LanguageFamily,
    trust: tuple[str, ...],
    program: tuple[str, ...] | None = None,
) -> ScopeProjection:
    return ScopeProjection.from_attribution(
        trust_inventory_paths=trust,
        attribution=NativeProgramAttribution(
            language=family,
            project_kind=ProjectKind.WORKSPACE_DEFAULT,
            selected_config_path=None,
            configured_program_paths=trust if program is None else program,
        ),
    )


def _factories(
    adapters: dict[LanguageFamily, _Adapter],
    contexts: dict[LanguageFamily, AdapterBuildContext],
    *,
    symbols: Sequence[Mapping[str, Any]] = (),
    on_snapshot: Callable[[], None] | None = None,
) -> dict[LanguageFamily, AdapterFactory]:
    def build(context: AdapterBuildContext) -> _Adapter:
        contexts[context.family] = context
        adapter = _Adapter(context, symbols=symbols, on_snapshot=on_snapshot)
        adapters[context.family] = adapter
        return adapter

    return cast(
        dict[LanguageFamily, AdapterFactory],
        {LanguageFamily.PYTHON: build, LanguageFamily.TYPESCRIPT: build},
    )


def test_composes_physical_key_and_skips_empty_language_family(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("value = 1\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    typescript_attributions = 0

    def unexpected_typescript(_root: Path, _paths: tuple[str, ...]) -> ScopeProjection:
        nonlocal typescript_attributions
        typescript_attributions += 1
        raise AssertionError("empty language families must be skipped")

    runtime = WorkspaceRuntime(
        (WorkspaceKind.GIT, tmp_path),
        path_policy=_PathPolicy(),
        inventory=_inventory(tmp_path, "main.py"),
        attributors={
            LanguageFamily.PYTHON: lambda _root, paths: _projection(LanguageFamily.PYTHON, paths),
            LanguageFamily.TYPESCRIPT: unexpected_typescript,
        },
        adapter_factories=_factories(adapters, contexts),
    )
    try:
        assert runtime.key == (WorkspaceKind.GIT, tmp_path.resolve())
        assert runtime.identity.working_subdirectory == tmp_path.resolve()
        assert runtime.route("main.py") is adapters[LanguageFamily.PYTHON]
        assert runtime.status()["skipped_language_families"] == ("typescript",)
        assert typescript_attributions == 0
        assert adapters[LanguageFamily.PYTHON].snapshot().phase is AdapterPhase.COLD
    finally:
        runtime.stop()


def test_scope_incompatibility_fails_before_executor_creation(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("value = 1\n")
    executor_creations = 0

    def incompatible(_root: Path, paths: tuple[str, ...]) -> ScopeProjection:
        return _projection(LanguageFamily.PYTHON, paths, (*paths, "ignored/generated.py"))

    def executor_factory(_root: Path) -> BoundedLspExecutor:
        nonlocal executor_creations
        executor_creations += 1
        return BoundedLspExecutor(queue_capacity=1, name="must-not-start")

    with pytest.raises(WorkspaceRuntimeError) as caught:
        WorkspaceRuntime(
            (WorkspaceKind.GIT, tmp_path),
            path_policy=_PathPolicy(),
            inventory=_inventory(tmp_path, "main.py"),
            attributors={LanguageFamily.PYTHON: incompatible},
            executor_factory=executor_factory,
        )

    assert caught.value.code is RuntimeErrorCode.SCOPE_INCOMPATIBLE
    assert caught.value.paths == ("ignored/generated.py",)
    assert executor_creations == 0


def test_constructor_failure_stops_already_built_adapter(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("value = 1\n")
    (tmp_path / "main.ts").write_text("export const value = 1\n")
    built: list[_Adapter] = []

    def build_python(context: AdapterBuildContext) -> _Adapter:
        adapter = _Adapter(context, symbols=())
        built.append(adapter)
        return adapter

    def fail_typescript(_context: AdapterBuildContext) -> _Adapter:
        raise RuntimeError("factory failed")

    with pytest.raises(RuntimeError, match="factory failed"):
        WorkspaceRuntime(
            (WorkspaceKind.GIT, tmp_path),
            path_policy=_PathPolicy(),
            inventory=_inventory(tmp_path, "main.py", "main.ts"),
            attributors={
                LanguageFamily.PYTHON: lambda _root, paths: _projection(LanguageFamily.PYTHON, paths),
                LanguageFamily.TYPESCRIPT: lambda _root, paths: _projection(LanguageFamily.TYPESCRIPT, paths),
            },
            adapter_factories=cast(
                dict[LanguageFamily, AdapterFactory],
                {
                    LanguageFamily.PYTHON: build_python,
                    LanguageFamily.TYPESCRIPT: fail_typescript,
                },
            ),
        )

    assert len(built) == 1
    assert built[0].stop_thread is not None
    assert built[0].context.executor.snapshot().stopping


def test_routes_over_one_executor_reports_status_and_stops_deterministically(tmp_path: Path) -> None:
    for relative in ("main.py", "main.ts", "extra.ts"):
        (tmp_path / relative).write_text("export const value = 1\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    runtime = WorkspaceRuntime(
        (WorkspaceKind.GIT, tmp_path),
        path_policy=_PathPolicy(),
        inventory=_inventory(tmp_path, "main.py", "main.ts", "extra.ts"),
        attributors={
            LanguageFamily.PYTHON: lambda _root, paths: _projection(LanguageFamily.PYTHON, paths),
            LanguageFamily.TYPESCRIPT: lambda _root, paths: _projection(
                LanguageFamily.TYPESCRIPT, paths, ("main.ts",)
            ),
        },
        adapter_factories=_factories(adapters, contexts),
    )

    assert runtime.route("main.py") is adapters[LanguageFamily.PYTHON]
    assert runtime.route("main.ts") is adapters[LanguageFamily.TYPESCRIPT]
    assert {id(context.executor) for context in contexts.values()} == {id(runtime.executor)}
    assert len({id(context.operation_lock) for context in contexts.values()}) == 1
    with pytest.raises(ValueError):
        runtime.route("../main.py")

    status = runtime.status()
    adapter_statuses = cast(Mapping[str, Mapping[str, Any]], status["adapters"])
    typescript = adapter_statuses["typescript"]
    assert typescript["raw_providers"]["implementation"] is True
    assert typescript["derived_tools"]["find_implementations"] is True
    assert typescript["engine"]["version"] == "1.0"
    assert typescript["position_encoding"] == "utf-16"
    assert typescript["trusted_not_in_configured_program"] == (
        {"path": "extra.ts", "reason": "omitted_by_engine_workspace_program"},
    )
    assert typescript["configured_program_outside_trust"] == ()
    executor_status = cast(Mapping[str, object], status["executor"])
    assert executor_status["queue_capacity"] == 32
    assert not any(word in repr(status).lower() for word in ("bearer", "password", "secret"))

    runtime.stop()
    assert all(adapter.stop_thread is not None for adapter in adapters.values())
    assert runtime.executor.snapshot().stopping
    runtime.stop()
    with pytest.raises(WorkspaceRuntimeError) as caught:
        runtime.route("main.py")
    assert caught.value.code is RuntimeErrorCode.STOPPED


def test_document_symbol_provider_preserves_snapshot_and_uses_futures(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    original = b"def before():\n    pass\n"
    source.write_bytes(original)
    symbol = {
        "name": "before",
        "kind": 12,
        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 1, "character": 8}},
        "selectionRange": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 10}},
    }
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    snapshot_taken = threading.Event()
    continue_operation = threading.Event()
    edit_completed = threading.Event()

    def pause_after_snapshot() -> None:
        snapshot_taken.set()
        assert continue_operation.wait(timeout=2.0)

    runtime = WorkspaceRuntime(
        (WorkspaceKind.GIT, tmp_path),
        path_policy=_PathPolicy(),
        inventory=_inventory(tmp_path, "main.py"),
        attributors={LanguageFamily.PYTHON: lambda _root, paths: _projection(LanguageFamily.PYTHON, paths)},
        adapter_factories=_factories(
            adapters,
            contexts,
            symbols=(symbol,),
            on_snapshot=pause_after_snapshot,
        ),
    )
    caller_thread = threading.current_thread().name
    try:
        loaded_holder: list[DocumentSymbolInput] = []
        load_thread = threading.Thread(
            target=lambda: loaded_holder.append(runtime.load_document_symbols("main.py")),
            name="tool-caller",
        )
        load_thread.start()
        assert snapshot_taken.wait(timeout=2.0)

        def ordered_edit() -> None:
            with contexts[LanguageFamily.PYTHON].operation_lock:
                source.write_text("def after():\n    pass\n")
                edit_completed.set()

        edit_thread = threading.Thread(target=ordered_edit, name="ordered-edit")
        edit_thread.start()
        assert not edit_completed.wait(timeout=0.05)
        continue_operation.set()
        load_thread.join(timeout=2.0)
        edit_thread.join(timeout=2.0)
        assert not load_thread.is_alive() and not edit_thread.is_alive()
        loaded = loaded_holder[0]
        adapter = adapters[LanguageFamily.PYTHON]
        assert loaded.snapshot.raw_bytes == original
        assert loaded.raw_symbols == (symbol,)
        assert loaded.position_encoding is PositionEncoding.UTF16
        assert loaded.workspace is not None and loaded.workspace.root == str(tmp_path.resolve())
        assert loaded.adapter is not None
        assert loaded.adapter.name == "pyright"
        assert loaded.adapter.language == "python"
        assert adapter.open_versions == [1]
        assert adapter.open_thread == adapter.probe_thread
        assert adapter.open_thread != caller_thread
        assert adapter.client.requests[0][0] == "textDocument/documentSymbol"
    finally:
        continue_operation.set()
        runtime.stop()
