from __future__ import annotations

import hashlib
import subprocess
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
from serena_light.workspace.inventory import SupportedPathTree, TrustInventory, git_trust_inventory
from serena_light.workspace.runtime import (
    AdapterBuildContext,
    AdapterFactory,
    FreshnessScan,
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
        self.notifications: list[tuple[str, object]] = []

    def request(self, method: str, params: object = None, *, timeout: float | None = None) -> object:
        self.requests.append((method, params, timeout))
        return self.symbols

    def notify(self, method: str, params: object = None) -> None:
        self.notifications.append((method, params))

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
        self.stop_calls = 0
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

    def submit_read(self, operation: Callable[[_Client], Any]) -> Future[Any]:
        return self.context.executor.submit(lambda: operation(self.client))

    def stop(self) -> Future[AdapterSnapshot]:
        self.stop_calls += 1

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
    *,
    selected_config_path: str | None = None,
) -> ScopeProjection:
    return ScopeProjection.from_attribution(
        trust_inventory_paths=trust,
        attribution=NativeProgramAttribution(
            language=family,
            project_kind=ProjectKind.CONFIGURED if selected_config_path is not None else ProjectKind.WORKSPACE_DEFAULT,
            selected_config_path=selected_config_path,
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


def test_all_incompatible_families_bind_for_status_and_fail_only_selected_scope(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("value = 1\n")
    executor_creations = 0

    def incompatible(_root: Path, paths: tuple[str, ...]) -> ScopeProjection:
        return _projection(LanguageFamily.PYTHON, paths, (*paths, "ignored/generated.py"))

    def executor_factory(_root: Path) -> BoundedLspExecutor:
        nonlocal executor_creations
        executor_creations += 1
        return BoundedLspExecutor(queue_capacity=1, name="unavailable-status")

    runtime = WorkspaceRuntime(
        (WorkspaceKind.GIT, tmp_path),
        path_policy=_PathPolicy(),
        inventory=_inventory(tmp_path, "main.py"),
        attributors={LanguageFamily.PYTHON: incompatible},
        executor_factory=executor_factory,
    )
    try:
        status = runtime.status()
        unavailable_families = cast(Mapping[str, Mapping[str, object]], status["unavailable_language_families"])
        unavailable = unavailable_families["python"]
        assert unavailable["error"] == {
            "code": RuntimeErrorCode.SCOPE_INCOMPATIBLE,
            "paths": ("ignored/generated.py",),
        }
        assert status["adapters"] == {}

        with pytest.raises(WorkspaceRuntimeError) as caught:
            runtime.route("main.py")
        assert caught.value.code is RuntimeErrorCode.SCOPE_INCOMPATIBLE
        assert caught.value.paths == ("ignored/generated.py",)
        assert runtime.find_symbol("Target").to_dict()["error"]["code"] == "SCOPE_INCOMPATIBLE"
    finally:
        runtime.stop()

    assert executor_creations == 1


def test_healthy_family_serves_while_other_family_is_scope_incompatible(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("value = 1\n")
    (tmp_path / "main.ts").write_text("export const value = 1\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}

    runtime = WorkspaceRuntime(
        (WorkspaceKind.GIT, tmp_path),
        path_policy=_PathPolicy(),
        inventory=_inventory(tmp_path, "main.py", "main.ts"),
        attributors={
            LanguageFamily.PYTHON: lambda _root, paths: _projection(LanguageFamily.PYTHON, paths),
            LanguageFamily.TYPESCRIPT: lambda _root, paths: _projection(
                LanguageFamily.TYPESCRIPT,
                paths,
                (*paths, "ignored/generated.ts"),
            ),
        },
        adapter_factories=_factories(adapters, contexts),
    )
    try:
        assert runtime.route("main.py") is adapters[LanguageFamily.PYTHON]
        assert LanguageFamily.TYPESCRIPT not in adapters
        with pytest.raises(WorkspaceRuntimeError) as caught:
            runtime.route("main.ts")
        assert caught.value.code is RuntimeErrorCode.SCOPE_INCOMPATIBLE
        unavailable = cast(Mapping[str, object], runtime.status()["unavailable_language_families"])
        assert "typescript" in unavailable
    finally:
        runtime.stop()


def test_family_can_recover_and_degrade_independently_after_reattribution(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("value = 1\n")
    compatible = [False]
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}

    def attribute(_root: Path, paths: tuple[str, ...]) -> ScopeProjection:
        program = paths if compatible[0] else (*paths, "ignored/generated.py")
        return _projection(LanguageFamily.PYTHON, paths, program)

    runtime = WorkspaceRuntime(
        (WorkspaceKind.GIT, tmp_path),
        path_policy=_PathPolicy(),
        inventory=_inventory(tmp_path, "main.py"),
        attributors={LanguageFamily.PYTHON: attribute},
        adapter_factories=_factories(adapters, contexts),
    )
    try:
        assert runtime.adapters == {}
        compatible[0] = True
        recovered = runtime.build_projections(runtime.inventory, {LanguageFamily.PYTHON})
        runtime.install_freshness(runtime.inventory, recovered)
        assert runtime.route("main.py") is adapters[LanguageFamily.PYTHON]

        compatible[0] = False
        degraded = runtime.build_projections(runtime.inventory, {LanguageFamily.PYTHON})
        runtime.install_freshness(runtime.inventory, degraded)
        assert runtime.adapters == {}
        assert adapters[LanguageFamily.PYTHON].stop_thread is not None
        with pytest.raises(WorkspaceRuntimeError) as caught:
            runtime.route("main.py")
        assert caught.value.code is RuntimeErrorCode.SCOPE_INCOMPATIBLE
    finally:
        runtime.stop()


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
    omitted = typescript["trusted_not_in_configured_program"]
    assert omitted["items"] == (
        {"path": "extra.ts", "reason": "omitted_by_engine_workspace_program"},
    )
    assert omitted["total"] == 1
    assert omitted["omitted_count"] == 0
    outside = typescript["configured_program_outside_trust"]
    assert outside["items"] == ()
    assert outside["total"] == 0
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


def _git_repository(root: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)


def _git_runtime(
    root: Path,
    adapters: dict[LanguageFamily, _Adapter],
    contexts: dict[LanguageFamily, AdapterBuildContext],
    *,
    inventory_factory: Callable[[Any], TrustInventory] | None = None,
    attributions: dict[LanguageFamily, int] | None = None,
    future_timeout: float = 35.0,
) -> WorkspaceRuntime:
    def attribute(family: LanguageFamily) -> Callable[[Path, tuple[str, ...]], ScopeProjection]:
        def attributor(_root: Path, paths: tuple[str, ...]) -> ScopeProjection:
            if attributions is not None:
                attributions[family] = attributions.get(family, 0) + 1
            return _projection(family, paths)

        return attributor

    return WorkspaceRuntime(
        (WorkspaceKind.GIT, root),
        path_policy=_PathPolicy(),
        inventory_factory=inventory_factory or (lambda identity: git_trust_inventory(identity.root)),
        attributors={
            LanguageFamily.PYTHON: attribute(LanguageFamily.PYTHON),
            LanguageFamily.TYPESCRIPT: attribute(LanguageFamily.TYPESCRIPT),
        },
        adapter_factories=_factories(adapters, contexts, symbols=[]),
        future_timeout=future_timeout,
    )


def test_freshness_reports_create_change_delete_and_notifies_running_adapters(tmp_path: Path) -> None:
    _git_repository(tmp_path)
    (tmp_path / "main.py").write_text("value = 1\n")
    (tmp_path / "gone.py").write_text("removed = 1\n")
    # A second language family exists so that reattribution can be shown to stay
    # scoped to the family whose membership actually moved.
    (tmp_path / "app.ts").write_text("export const value = 1;\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    attributions: dict[LanguageFamily, int] = {}
    runtime = _git_runtime(tmp_path, adapters, contexts, attributions=attributions)
    try:
        # Loading one document is what makes the fake adapter running, which is
        # the precondition for receiving watcher notifications at all.
        runtime.load_document_symbols("main.py")
        python = adapters[LanguageFamily.PYTHON]
        python.client.notifications.clear()
        before = contexts[LanguageFamily.PYTHON].scope_tracker.generations
        typescript_before = contexts[LanguageFamily.TYPESCRIPT].scope_tracker.generations

        (tmp_path / "main.py").write_text("value = 22222\n")
        (tmp_path / "created.py").write_text("fresh = 1\n")
        (tmp_path / "gone.py").unlink()
        scan = runtime.ensure_fresh()

        assert scan.created == ("created.py",)
        assert scan.changed == ("main.py",)
        assert scan.deleted == ("gone.py",)
        assert scan.reattributed == (LanguageFamily.PYTHON,)
        assert scan.notified == (LanguageFamily.PYTHON,)
        assert runtime.inventory.paths == ("app.ts", "created.py", "main.py")
        assert attributions == {LanguageFamily.PYTHON: 2, LanguageFamily.TYPESCRIPT: 1}

        runtime.executor.submit(lambda: None).result(timeout=5)
        methods = [method for method, _ in python.client.notifications]
        assert methods == ["workspace/didChangeWatchedFiles", "textDocument/didOpen", "textDocument/didClose"]
        changes = cast(Mapping[str, Any], python.client.notifications[0][1])["changes"]
        assert {(item["uri"].rsplit("/", 1)[-1], item["type"]) for item in changes} == {
            ("created.py", 1),
            ("main.py", 2),
            ("gone.py", 3),
        }
        opened = cast(Mapping[str, Any], python.client.notifications[1][1])["textDocument"]
        assert opened["uri"] == (tmp_path / "created.py").resolve().as_uri()
        assert opened["languageId"] == "python"
        assert opened["text"] == "fresh = 1\n"

        after = contexts[LanguageFamily.PYTHON].scope_tracker.generations
        assert after.trust_inventory > before.trust_inventory
        assert after.configured_program > before.configured_program
        assert after.path_scoped["main.py"] > before.path_scoped.get("main.py", 0)

        # Python churn must not invalidate the TypeScript configured program.
        typescript = contexts[LanguageFamily.TYPESCRIPT].scope_tracker.generations
        assert typescript.configured_program == typescript_before.configured_program
        assert typescript.trust_inventory == typescript_before.trust_inventory
        assert typescript.path_scoped == typescript_before.path_scoped
    finally:
        runtime.stop()


def test_concurrent_freshness_callers_share_one_scan_and_no_time_cache_authorizes_reuse(
    tmp_path: Path,
) -> None:
    _git_repository(tmp_path)
    (tmp_path / "main.py").write_text("value = 1\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    entered = threading.Event()
    release = threading.Event()
    rebuilds = 0

    def blocking_inventory(identity: Any) -> TrustInventory:
        nonlocal rebuilds
        rebuilds += 1
        if rebuilds > 1:
            entered.set()
            assert release.wait(5)
        return git_trust_inventory(identity.root)

    runtime = _git_runtime(tmp_path, adapters, contexts, inventory_factory=blocking_inventory)
    try:
        results: list[Any] = []
        first = threading.Thread(target=lambda: results.append(runtime.ensure_fresh()))
        first.start()
        assert entered.wait(5)
        joined = threading.Thread(target=lambda: results.append(runtime.ensure_fresh()))
        joined.start()
        # The joined caller must not start a second rebuild while one is running.
        assert not release.wait(0.2)
        assert rebuilds == 2
        release.set()
        first.join(timeout=5)
        joined.join(timeout=5)

        assert len(results) == 2
        assert results[0] is results[1]
        # A completed scan is never reused: the next operation rebuilds again.
        runtime.ensure_fresh()
        assert rebuilds == 3
    finally:
        runtime.stop()


def test_freshness_detects_symlink_substitution_and_native_config_change(tmp_path: Path) -> None:
    _git_repository(tmp_path)
    (tmp_path / "main.py").write_text("value = 1\n")
    (tmp_path / "other.py").write_text("value = 2\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    runtime = _git_runtime(tmp_path, adapters, contexts)
    try:
        runtime.ensure_fresh()
        (tmp_path / "main.py").unlink()
        (tmp_path / "main.py").symlink_to(tmp_path / "other.py")
        scan = runtime.ensure_fresh()

        assert scan.symlinked == ("main.py",)
        assert scan.deleted == ("main.py",)
        assert "main.py" not in runtime.inventory.paths
        assert ("main.py", "symlink") in {(item.path, item.reason) for item in runtime.inventory.rejected}

        (tmp_path / "pyrightconfig.json").write_text("{}\n")
        config_scan = runtime.ensure_fresh()

        assert config_scan.config_changed == ("pyrightconfig.json",)
        assert config_scan.reattributed == (LanguageFamily.PYTHON,)
    finally:
        runtime.stop()


def test_nested_native_config_restarts_only_its_running_adapter_before_new_readiness(tmp_path: Path) -> None:
    _git_repository(tmp_path)
    package = tmp_path / "package"
    package.mkdir()
    (package / "main.ts").write_text("export const value = 1;\n")
    (tmp_path / "main.py").write_text("value = 1\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    attributions: dict[LanguageFamily, int] = {}

    def typescript_attribute(_root: Path, paths: tuple[str, ...]) -> ScopeProjection:
        attributions[LanguageFamily.TYPESCRIPT] = attributions.get(LanguageFamily.TYPESCRIPT, 0) + 1
        selected = "package/tsconfig.json" if (package / "tsconfig.json").exists() else None
        return _projection(LanguageFamily.TYPESCRIPT, paths, selected_config_path=selected)

    runtime = WorkspaceRuntime(
        (WorkspaceKind.GIT, tmp_path),
        path_policy=_PathPolicy(),
        inventory_factory=lambda identity: git_trust_inventory(identity.root),
        attributors={
            LanguageFamily.PYTHON: lambda _root, paths: _projection(LanguageFamily.PYTHON, paths),
            LanguageFamily.TYPESCRIPT: typescript_attribute,
        },
        adapter_factories=_factories(adapters, contexts, symbols=[]),
    )
    try:
        runtime.load_document_symbols("main.py")
        runtime.load_document_symbols("package/main.ts")
        python = adapters[LanguageFamily.PYTHON]
        original_typescript = adapters[LanguageFamily.TYPESCRIPT]
        python_generations = python.snapshot().generations
        original_typescript_program = original_typescript.snapshot().generations.program

        # The candidate was absent at activation.  Its later creation is still
        # detected because source-directory ancestry is part of the watch set.
        (package / "tsconfig.json").write_text('{"include": ["*.ts"]}\n')
        created_config = runtime.ensure_fresh()
        replacement = adapters[LanguageFamily.TYPESCRIPT]

        assert created_config.config_changed == ("package/tsconfig.json",)
        assert created_config.reattributed == (LanguageFamily.TYPESCRIPT,)
        assert replacement is not original_typescript
        assert original_typescript.stop_thread is not None
        assert replacement.snapshot().phase is AdapterPhase.COLD
        assert not replacement.snapshot().running
        assert replacement.context.projection.selected_config_path == "package/tsconfig.json"
        assert replacement.context.scope_tracker is original_typescript.context.scope_tracker
        assert replacement.snapshot().generations.program > original_typescript_program
        assert adapters[LanguageFamily.PYTHON] is python
        assert python.snapshot().generations == python_generations

        # A replacement cannot reuse the old adapter's document-ready state.
        runtime.load_document_symbols("package/main.ts")
        assert replacement.snapshot().phase is AdapterPhase.DOCUMENT_READY
        assert replacement.open_versions == [2]
        replacement_program = replacement.snapshot().generations.program

        (package / "tsconfig.json").write_text('{"include": ["main.ts"], "strict": true}\n')
        changed_config = runtime.ensure_fresh()
        second_replacement = adapters[LanguageFamily.TYPESCRIPT]

        assert changed_config.config_changed == ("package/tsconfig.json",)
        assert changed_config.reattributed == (LanguageFamily.TYPESCRIPT,)
        assert second_replacement is not replacement
        assert replacement.stop_thread is not None
        assert second_replacement.snapshot().phase is AdapterPhase.COLD
        assert not second_replacement.snapshot().running
        assert second_replacement.context.scope_tracker is replacement.context.scope_tracker
        assert second_replacement.snapshot().generations.program > replacement_program
        assert adapters[LanguageFamily.PYTHON] is python
        assert python.snapshot().generations == python_generations
        assert attributions == {LanguageFamily.TYPESCRIPT: 3}
    finally:
        runtime.stop()


def test_config_restart_timeout_is_explicit_and_retries_same_stop_before_recovery(tmp_path: Path) -> None:
    _git_repository(tmp_path)
    (tmp_path / "main.py").write_text("value = 1\n")
    (tmp_path / "main.ts").write_text("export const value = 1;\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    runtime = _git_runtime(tmp_path, adapters, contexts, future_timeout=0.05)
    release = threading.Event()
    entered = threading.Event()
    try:
        runtime.load_document_symbols("main.py")
        runtime.load_document_symbols("main.ts")
        original_python = adapters[LanguageFamily.PYTHON]
        original_typescript = adapters[LanguageFamily.TYPESCRIPT]
        python_program_before = original_python.snapshot().generations.program
        typescript_generations = original_typescript.snapshot().generations

        def block_executor() -> None:
            entered.set()
            assert release.wait(5)

        blocked = runtime.executor.submit(block_executor)
        assert entered.wait(5)
        (tmp_path / "pyrightconfig.json").write_text('{"include": ["*.py"]}\n')

        with pytest.raises(TimeoutError):
            runtime.ensure_fresh()

        assert LanguageFamily.PYTHON not in runtime.adapters
        unavailable = cast(Mapping[str, object], runtime.status()["unavailable_language_families"])
        python_error = cast(Mapping[str, object], cast(Mapping[str, object], unavailable["python"])["error"])
        assert python_error["code"] == "TIMED_OUT"
        assert runtime.adapters[LanguageFamily.TYPESCRIPT] is original_typescript
        assert original_typescript.snapshot().generations == typescript_generations

        timed_out = runtime.get_symbols_overview("main.py").to_dict()
        assert timed_out["error"]["code"] == "TIMED_OUT"
        assert timed_out["error"]["retry"]["retryable"] is True

        release.set()
        blocked.result(timeout=5)
        recovered_scan = runtime.ensure_fresh()
        replacement = adapters[LanguageFamily.PYTHON]

        assert recovered_scan.config_changed == ()
        assert runtime.adapters[LanguageFamily.PYTHON] is replacement
        assert replacement is not original_python
        assert replacement.snapshot().phase is AdapterPhase.COLD
        assert replacement.snapshot().generations.program == python_program_before + 1
        assert "python" not in runtime.status()["unavailable_language_families"]
        assert runtime.adapters[LanguageFamily.TYPESCRIPT] is original_typescript
        assert original_typescript.snapshot().generations == typescript_generations
    finally:
        release.set()
        runtime.stop()


def test_runtime_stop_settles_pending_restart_once_without_publishing_replacement(tmp_path: Path) -> None:
    _git_repository(tmp_path)
    (tmp_path / "main.py").write_text("value = 1\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    runtime = WorkspaceRuntime(
        (WorkspaceKind.GIT, tmp_path),
        path_policy=_PathPolicy(),
        inventory_factory=lambda identity: git_trust_inventory(identity.root),
        attributors={
            LanguageFamily.PYTHON: lambda _root, paths: _projection(LanguageFamily.PYTHON, paths)
        },
        adapter_factories=_factories(adapters, contexts, symbols=[]),
        future_timeout=0.2,
    )
    unblock = threading.Event()
    unblocked = threading.Event()
    finish = threading.Event()
    stop_failures: list[BaseException] = []
    stopped = False
    try:
        runtime.load_document_symbols("main.py")
        original = adapters[LanguageFamily.PYTHON]

        def block_executor() -> None:
            assert unblock.wait(5)
            unblocked.set()
            assert finish.wait(5)

        runtime.executor.submit(block_executor)
        (tmp_path / "pyrightconfig.json").write_text('{"include": ["*.py"]}\n')
        with pytest.raises(TimeoutError):
            runtime.ensure_fresh()
        assert original.stop_calls == 1
        assert original.stop_thread is None
        assert LanguageFamily.PYTHON not in runtime.adapters

        unblock.set()
        assert unblocked.wait(5)

        def stop_runtime() -> None:
            try:
                runtime.stop()
            except BaseException as error:
                stop_failures.append(error)

        stopping = threading.Thread(target=stop_runtime)
        stopping.start()
        assert stopping.is_alive()
        finish.set()
        stopping.join(timeout=5)
        assert not stopping.is_alive()
        stopped = True

        assert stop_failures == []
        assert original.stop_calls == 1
        assert original.stop_thread is not None
        assert adapters[LanguageFamily.PYTHON] is original
        assert runtime.adapters == {}
    finally:
        unblock.set()
        finish.set()
        if not stopped:
            runtime.stop()


def test_read_only_non_git_root_uses_targeted_stat_instead_of_a_full_scan(tmp_path: Path) -> None:
    source = tmp_path / "main.py"
    source.write_text("value = 1\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    rebuilds = 0

    def counted_inventory(identity: Any) -> TrustInventory:
        nonlocal rebuilds
        rebuilds += 1
        return _inventory(identity.root, "main.py")

    runtime = WorkspaceRuntime(
        (WorkspaceKind.ALLOWLISTED_NON_GIT, tmp_path),
        path_policy=_PathPolicy(),
        inventory_factory=counted_inventory,
        attributors={LanguageFamily.PYTHON: lambda _root, paths: _projection(LanguageFamily.PYTHON, paths)},
        adapter_factories=_factories(adapters, contexts),
    )
    try:
        runtime.load_document_symbols("main.py")
        python = adapters[LanguageFamily.PYTHON]
        python.client.notifications.clear()
        rebuilds_after_construction = rebuilds
        before = contexts[LanguageFamily.PYTHON].scope_tracker.generations

        assert runtime.ensure_fresh() == FreshnessScan()
        source.write_text("value = 22222\n")
        runtime.load_document_symbols("main.py")
        runtime.executor.submit(lambda: None).result(timeout=5)

        # The allowlisted root is never re-walked; only the named operand is stat-ed.
        assert rebuilds == rebuilds_after_construction
        after = contexts[LanguageFamily.PYTHON].scope_tracker.generations
        assert after.path_scoped["main.py"] > before.path_scoped.get("main.py", 0)
        assert [method for method, _ in python.client.notifications] == ["workspace/didChangeWatchedFiles"]
    finally:
        runtime.stop()
