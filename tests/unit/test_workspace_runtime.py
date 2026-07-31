from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from pathlib import Path, PurePosixPath
from typing import Any, cast

import pytest

import serena_light.workspace.inventory as inventory_module
from serena_light.lsp.adapter import (
    AdapterErrorCode,
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
    ReadinessWitnessError,
)
from serena_light.lsp.client import LspProtocolError, LspResponseError, LspTransportClosed
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
    FileChangeType,
    LanguageFamily,
    NativeProgramAttribution,
    ProjectKind,
    ScopeProjection,
    WatchedFileEvent,
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


def _stat_with_fixed_times(observed: os.stat_result) -> os.stat_result:
    return os.stat_result(
        (
            observed.st_mode,
            observed.st_ino,
            observed.st_dev,
            observed.st_nlink,
            observed.st_uid,
            observed.st_gid,
            observed.st_size,
            0,
            0,
            0,
        )
    )


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
        self.before_stop_submit: Callable[[], None] | None = None
        self.before_stop_worker: Callable[[], None] | None = None
        self.before_reconcile_worker: Callable[[], None] | None = None
        self.warm_global_error: BaseException | None = None
        self._phase = AdapterPhase.COLD
        self._document_generation = 0
        self._running = False
        self._open_documents: dict[str, str] = {}

    def routes(self, path: str | Path) -> bool:
        suffix = PurePosixPath(str(path)).suffix.lower()
        if self.context.family is LanguageFamily.PYTHON:
            return suffix in {".py", ".pyi"}
        return suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}

    def snapshot(self) -> AdapterSnapshot:
        scope = self.context.scope_tracker.generations
        if self._phase is AdapterPhase.READY and scope.observed_configured_program < scope.configured_program:
            self._phase = AdapterPhase.GLOBAL_WARMING
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
                self._open_documents[relative_path] = uri
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

    def warm_global(
        self, witness: GlobalReadinessWitness, *, timeout: float | None = None
    ) -> Future[tuple[Mapping[str, object], ...]]:
        del witness, timeout

        def warm() -> tuple[Mapping[str, object], ...]:
            if self.warm_global_error is not None:
                raise self.warm_global_error
            generation = self.context.scope_tracker.generations.configured_program
            assert self.context.scope_tracker.observe_configured_program(generation)
            self._phase = AdapterPhase.READY
            return tuple(self.client.symbols)

        return self.context.executor.submit(warm)

    def reconcile_watched_files(
        self,
        *,
        events: Sequence[WatchedFileEvent],
        created: Sequence[str],
        versions: Mapping[str, int],
    ) -> Future[None]:
        """Minimal deterministic model of production open-buffer reconciliation."""

        def send() -> None:
            if self.before_reconcile_worker is not None:
                self.before_reconcile_worker()
            workspace_uri = self.context.workspace_root.as_uri()
            self.client.notify(
                "workspace/didChangeWatchedFiles",
                {"changes": [dict(event.as_lsp_change(workspace_uri)) for event in events]},
            )
            changed = {event.path for event in events if event.change_type is FileChangeType.CHANGED}
            deleted = {event.path for event in events if event.change_type is FileChangeType.DELETED}
            for relative_path, uri in tuple(self._open_documents.items()):
                if relative_path in deleted:
                    self.client.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
                    del self._open_documents[relative_path]
                    continue
                if relative_path not in changed:
                    continue
                self._document_generation += 1
                self.client.notify(
                    "textDocument/didChange",
                    {
                        "textDocument": {"uri": uri, "version": versions[relative_path]},
                        "contentChanges": [
                            {"text": (self.context.workspace_root / relative_path).read_text(encoding="utf-8")}
                        ],
                    },
                )
            for relative_path in created:
                path = self.context.workspace_root / relative_path
                uri = path.as_uri()
                language_id = "python" if path.suffix in {".py", ".pyi"} else "typescript"
                self.client.notify(
                    "textDocument/didOpen",
                    {
                        "textDocument": {
                            "uri": uri,
                            "languageId": language_id,
                            "version": 1,
                            "text": path.read_text(encoding="utf-8"),
                        }
                    },
                )
                self.client.notify("textDocument/didClose", {"textDocument": {"uri": uri}})

        return self.context.executor.submit(send)

    def stop(self) -> Future[AdapterSnapshot]:
        self.stop_calls += 1
        if self.before_stop_submit is not None:
            self.before_stop_submit()

        def stop_on_executor() -> AdapterSnapshot:
            self.stop_thread = threading.current_thread().name
            if self.before_stop_worker is not None:
                self.before_stop_worker()
            self._phase = AdapterPhase.STOPPING
            self._running = False
            return self.snapshot()

        return self.context.executor.submit_cleanup(stop_on_executor)


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


def test_incompatible_reattribution_and_runtime_stop_share_cleanup_owner(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("value = 1\n")
    compatible = [True]
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
    submit_entered = threading.Event()
    allow_submit = threading.Event()
    refresh_errors: list[BaseException] = []
    stop_errors: list[BaseException] = []
    try:
        original = adapters[LanguageFamily.PYTHON]

        def block_submit() -> None:
            submit_entered.set()
            assert allow_submit.wait(5)

        original.before_stop_submit = block_submit
        compatible[0] = False
        degraded = runtime.build_projections(runtime.inventory, {LanguageFamily.PYTHON})

        def refresh() -> None:
            try:
                runtime.install_freshness(runtime.inventory, degraded)
            except BaseException as error:
                refresh_errors.append(error)

        refreshing = threading.Thread(target=refresh)
        refreshing.start()
        assert submit_entered.wait(5)

        def stop_runtime() -> None:
            try:
                runtime.stop()
            except BaseException as error:
                stop_errors.append(error)

        stopping = threading.Thread(target=stop_runtime)
        stopping.start()
        stopping.join(timeout=0.1)
        assert stopping.is_alive()

        allow_submit.set()
        refreshing.join(timeout=5)
        stopping.join(timeout=5)

        assert not refreshing.is_alive()
        assert not stopping.is_alive()
        assert refresh_errors == []
        assert stop_errors == []
        assert original.stop_calls == 1
        assert runtime._pending_retirements == {}
        assert runtime.adapters == {}
        assert runtime.status()["stopped"] is True
    finally:
        allow_submit.set()
        runtime.stop()


def test_incompatible_retirement_retries_rejected_cleanup_admission(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("value = 1\n")
    compatible = [True]
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
    attempts = 0
    try:
        original = adapters[LanguageFamily.PYTHON]

        def reject_once() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("cleanup admission rejected")

        original.before_stop_submit = reject_once
        compatible[0] = False
        degraded = runtime.build_projections(runtime.inventory, {LanguageFamily.PYTHON})

        with pytest.raises(WorkspaceRuntimeError) as caught:
            runtime.install_freshness(runtime.inventory, degraded)
        assert caught.value.code is RuntimeErrorCode.UNSUPPORTED
        assert runtime.status()["stopped"] is False
        assert runtime._pending_retirements[LanguageFamily.PYTHON].stop_adapter is original

        runtime.stop()
        assert attempts == 2
        assert original.stop_calls == 2
        assert runtime._pending_retirements == {}
        assert runtime.status()["stopped"] is True
    finally:
        runtime.stop()


def test_incompatible_retirement_retries_failed_cleanup_future_on_next_scan(tmp_path: Path) -> None:
    _git_repository(tmp_path)
    source = tmp_path / "main.py"
    source.write_text("value = 1\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    runtime = _git_runtime(tmp_path, adapters, contexts)
    try:
        original = adapters[LanguageFamily.PYTHON]
        attempts = 0

        def fail_worker_once() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("cleanup worker failed")

        original.before_stop_worker = fail_worker_once
        source.unlink()

        with pytest.raises(RuntimeError, match="cleanup worker failed"):
            runtime.ensure_fresh()
        assert original.stop_calls == 1
        assert runtime.status()["stopped"] is False
        assert runtime.adapters == {}
        unavailable = cast(Mapping[str, object], runtime.status()["unavailable_language_families"])
        python = cast(Mapping[str, object], unavailable["python"])
        assert cast(Mapping[str, object], python["error"])["code"] == "SCOPE_INCOMPATIBLE"

        assert runtime.ensure_fresh().dirty is False
        assert original.stop_calls == 2
        assert runtime._pending_retirements == {}
        assert runtime.status()["stopped"] is False
        assert runtime.adapters == {}
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
    executor_factory: Callable[[Path], BoundedLspExecutor] | None = None,
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
        executor_factory=executor_factory,
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
        assert methods == [
            "workspace/didChangeWatchedFiles",
            "textDocument/didChange",
            "textDocument/didOpen",
            "textDocument/didClose",
        ]
        changes = cast(Mapping[str, Any], python.client.notifications[0][1])["changes"]
        assert {(item["uri"].rsplit("/", 1)[-1], item["type"]) for item in changes} == {
            ("created.py", 1),
            ("main.py", 2),
            ("gone.py", 3),
        }
        refreshed = cast(Mapping[str, Any], python.client.notifications[1][1])
        assert refreshed["textDocument"] == {"uri": (tmp_path / "main.py").resolve().as_uri(), "version": 2}
        assert refreshed["contentChanges"] == [{"text": "value = 22222\n"}]
        opened = cast(Mapping[str, Any], python.client.notifications[2][1])["textDocument"]
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


def test_full_ordinary_queue_keeps_freshness_pending_until_an_unchanged_retry_can_reconcile(tmp_path: Path) -> None:
    _git_repository(tmp_path)
    (tmp_path / "main.py").write_text("value = 1\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    runtime = _git_runtime(
        tmp_path,
        adapters,
        contexts,
        executor_factory=lambda _root: BoundedLspExecutor(queue_capacity=1, name="freshness-admission"),
    )
    entered = threading.Event()
    release = threading.Event()
    try:
        runtime.load_document_symbols("main.py")
        python = adapters[LanguageFamily.PYTHON]
        python.client.notifications.clear()

        def block_worker() -> None:
            entered.set()
            assert release.wait(5)

        active = runtime.executor.submit(block_worker)
        assert entered.wait(5)
        queued = runtime.executor.submit(lambda: None)
        (tmp_path / "main.py").write_text("value = 22222\n")

        busy = runtime.get_symbols_overview("main.py").to_dict()
        assert busy["error"]["code"] == "BUSY"
        assert busy["error"]["retry"]["retryable"] is True
        assert runtime.freshness._pending_reconciles[LanguageFamily.PYTHON].future is None

        release.set()
        active.result(timeout=5)
        queued.result(timeout=5)
        recovered = runtime.get_symbols_overview("main.py").to_dict()

        assert recovered["ok"] is True
        assert LanguageFamily.PYTHON not in runtime.freshness._pending_reconciles
        assert [method for method, _ in python.client.notifications] == [
            "workspace/didChangeWatchedFiles",
            "textDocument/didChange",
        ]
    finally:
        release.set()
        runtime.stop()


def test_failed_reconcile_future_blocks_current_generation_until_a_later_retry_succeeds(tmp_path: Path) -> None:
    _git_repository(tmp_path)
    (tmp_path / "main.py").write_text("value = 1\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    runtime = _git_runtime(tmp_path, adapters, contexts)
    failed_once = True
    try:
        runtime.load_document_symbols("main.py")
        python = adapters[LanguageFamily.PYTHON]
        python.client.notifications.clear()

        def fail_once() -> None:
            nonlocal failed_once
            if failed_once:
                failed_once = False
                raise RuntimeError("watcher notification failed")

        python.before_reconcile_worker = fail_once
        (tmp_path / "main.py").write_text("value = 2\n")

        not_ready = runtime.get_symbols_overview("main.py").to_dict()
        assert not_ready["error"]["code"] == "NOT_READY"
        assert LanguageFamily.PYTHON in runtime.freshness._pending_reconciles
        assert python.client.notifications == []

        recovered = runtime.get_symbols_overview("main.py").to_dict()

        assert recovered["ok"] is True
        assert LanguageFamily.PYTHON not in runtime.freshness._pending_reconciles
        assert [method for method, _ in python.client.notifications] == [
            "workspace/didChangeWatchedFiles",
            "textDocument/didChange",
        ]
    finally:
        runtime.stop()


def test_two_family_reconcile_failure_invalidates_all_families_and_retries_exact_batch(tmp_path: Path) -> None:
    _git_repository(tmp_path)
    (tmp_path / "main.py").write_text("value = 1\n")
    (tmp_path / "main.ts").write_text("export const value = 1;\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    runtime = _git_runtime(tmp_path, adapters, contexts)
    failed_once = True
    try:
        runtime.load_document_symbols("main.py")
        runtime.load_document_symbols("main.ts")
        for family, adapter in adapters.items():
            generation = contexts[family].scope_tracker.generations.configured_program
            assert contexts[family].scope_tracker.observe_configured_program(generation)
            adapter._phase = AdapterPhase.READY
            adapter.client.notifications.clear()

        python = adapters[LanguageFamily.PYTHON]
        typescript = adapters[LanguageFamily.TYPESCRIPT]
        typescript_before = contexts[LanguageFamily.TYPESCRIPT].scope_tracker.generations

        def fail_once() -> None:
            nonlocal failed_once
            if failed_once:
                failed_once = False
                raise RuntimeError("first-family watcher notification failed")

        python.before_reconcile_worker = fail_once
        (tmp_path / "main.py").write_text("value = 22222\n")
        (tmp_path / "main.ts").write_text("export const value = 22222;\n")

        with pytest.raises(WorkspaceRuntimeError) as caught:
            runtime.ensure_fresh()

        assert caught.value.code is RuntimeErrorCode.NOT_READY
        typescript_after = contexts[LanguageFamily.TYPESCRIPT].scope_tracker.generations
        assert typescript_after.configured_program == typescript_before.configured_program + 1
        assert typescript_after.observed_configured_program == typescript_before.observed_configured_program
        assert typescript.snapshot().phase is AdapterPhase.GLOBAL_WARMING
        assert runtime.freshness._pending_reconciles[LanguageFamily.PYTHON].future is None
        assert LanguageFamily.TYPESCRIPT not in runtime.freshness._pending_reconciles
        assert [method for method, _ in typescript.client.notifications] == [
            "workspace/didChangeWatchedFiles",
            "textDocument/didChange",
        ]

        assert runtime.ensure_fresh() == FreshnessScan()
        assert runtime.freshness._pending_reconciles == {}
        assert [method for method, _ in python.client.notifications] == [
            "workspace/didChangeWatchedFiles",
            "textDocument/didChange",
        ]
        assert [method for method, _ in typescript.client.notifications] == [
            "workspace/didChangeWatchedFiles",
            "textDocument/didChange",
        ]
    finally:
        runtime.stop()


@pytest.mark.parametrize(
    "failure",
    [
        LspResponseError(-32603, "server rejected global warm-up"),
        LspProtocolError("malformed global warm-up response"),
        LspTransportClosed("global warm-up transport closed"),
    ],
)
def test_cold_global_lookup_propagates_non_readiness_lsp_failures(tmp_path: Path, failure: BaseException) -> None:
    _git_repository(tmp_path)
    (tmp_path / "main.py").write_text("Target = 1\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    symbols = ({"name": "Target", "kind": 13},)
    runtime = WorkspaceRuntime(
        (WorkspaceKind.GIT, tmp_path),
        path_policy=_PathPolicy(),
        inventory_factory=lambda identity: git_trust_inventory(identity.root),
        attributors={
            LanguageFamily.PYTHON: lambda _root, paths: _projection(LanguageFamily.PYTHON, paths)
        },
        adapter_factories=_factories(adapters, contexts, symbols=symbols),
        future_timeout=0.1,
    )
    try:
        adapters[LanguageFamily.PYTHON].warm_global_error = failure

        with pytest.raises(type(failure), match=str(failure).split(" (")[0]):
            runtime.find_symbol("Target")
    finally:
        runtime.stop()


def test_cold_global_lookup_keeps_readiness_witness_failure_as_not_ready(tmp_path: Path) -> None:
    _git_repository(tmp_path)
    (tmp_path / "main.py").write_text("Target = 1\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    symbols = ({"name": "Target", "kind": 13},)
    runtime = WorkspaceRuntime(
        (WorkspaceKind.GIT, tmp_path),
        path_policy=_PathPolicy(),
        inventory_factory=lambda identity: git_trust_inventory(identity.root),
        attributors={LanguageFamily.PYTHON: lambda _root, paths: _projection(LanguageFamily.PYTHON, paths)},
        adapter_factories=_factories(adapters, contexts, symbols=symbols),
        future_timeout=0.01,
    )
    try:
        adapters[LanguageFamily.PYTHON].warm_global_error = ReadinessWitnessError(
            AdapterErrorCode.NOT_READY,
            "sentinel is not indexed yet",
            retry_after_seconds=0.1,
        )

        result = runtime.find_symbol("Target").to_dict()

        assert result["error"]["code"] == "NOT_READY"
        assert result["error"]["retry"]["retryable"] is True
    finally:
        runtime.stop()


def test_call_arriving_during_a_scan_waits_then_runs_its_own_distinct_scan(
    tmp_path: Path,
) -> None:
    _git_repository(tmp_path)
    (tmp_path / "main.py").write_text("value = 1\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    first_entered = threading.Event()
    first_release = threading.Event()
    second_entered = threading.Event()
    second_release = threading.Event()
    rebuilds = 0

    def blocking_inventory(identity: Any) -> TrustInventory:
        nonlocal rebuilds
        rebuilds += 1
        # Call 1 is the constructor's baseline build; calls 2 and 3 are the
        # ensure_fresh scans for call A and call B respectively.
        if rebuilds == 2:
            first_entered.set()
            assert first_release.wait(5)
        elif rebuilds == 3:
            second_entered.set()
            assert second_release.wait(5)
        return git_trust_inventory(identity.root)

    runtime = _git_runtime(tmp_path, adapters, contexts, inventory_factory=blocking_inventory)
    try:
        results: dict[str, Any] = {}
        call_a = threading.Thread(target=lambda: results.__setitem__("a", runtime.ensure_fresh()))
        call_a.start()
        assert first_entered.wait(5)

        # Give A's eventual scan a distinguishable, non-empty result so a
        # regressed or overwritten ``_last`` commit is observable below.
        (tmp_path / "main.py").write_text("value = 2\n")

        call_b = threading.Thread(target=lambda: results.__setitem__("b", runtime.ensure_fresh()))
        call_b.start()
        # Exact ticket-issued barrier: wait on the same condition the admission
        # queue itself notifies on, rather than polling or guessing a duration.
        condition = runtime.freshness._admission_condition
        with condition:
            ticket_issued = condition.wait_for(lambda: runtime.freshness._next_ticket >= 2, timeout=5)
        assert ticket_issued
        # B's ticket is issued but A's scan has not settled, so B must still be
        # waiting rather than having accepted A's in-progress scan.
        assert not second_entered.is_set()
        assert rebuilds == 2

        first_release.set()
        call_a.join(timeout=5)
        # Only after A's scan settles does B's own distinct scan begin.
        assert second_entered.wait(5)
        assert rebuilds == 3
        # A's result must already be committed before B's scan is admitted:
        # the commit-then-release ordering means B can never observe or cause
        # a regression of ``_last`` back to a stale value.
        assert runtime.freshness.last_scan == results["a"]
        assert results["a"].changed == ("main.py",)

        second_release.set()
        call_b.join(timeout=5)

        assert rebuilds == 3
        assert results["a"] is not results["b"]
        assert runtime.freshness.last_scan == results["b"]
    finally:
        runtime.stop()


def test_a_failed_scan_propagates_only_to_its_own_caller_and_unblocks_the_next_ticket(
    tmp_path: Path,
) -> None:
    _git_repository(tmp_path)
    (tmp_path / "main.py").write_text("value = 1\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    first_entered = threading.Event()
    first_release = threading.Event()
    rebuilds = 0

    class _Boom(RuntimeError):
        pass

    def blocking_inventory(identity: Any) -> TrustInventory:
        nonlocal rebuilds
        rebuilds += 1
        # Call 1 is the constructor's baseline build; call 2 is call A's scan.
        if rebuilds == 2:
            first_entered.set()
            assert first_release.wait(5)
            raise _Boom("scan failed")
        return git_trust_inventory(identity.root)

    runtime = _git_runtime(tmp_path, adapters, contexts, inventory_factory=blocking_inventory)
    try:
        errors: dict[str, Any] = {}
        results: dict[str, Any] = {}

        def call_a() -> None:
            try:
                runtime.ensure_fresh()
            except BaseException as caught:
                errors["a"] = caught

        def call_b() -> None:
            results["b"] = runtime.ensure_fresh()

        thread_a = threading.Thread(target=call_a)
        thread_a.start()
        assert first_entered.wait(5)

        thread_b = threading.Thread(target=call_b)
        thread_b.start()
        # Exact ticket-issued barrier: wait on the same condition the admission
        # queue itself notifies on, rather than polling or guessing a duration.
        condition = runtime.freshness._admission_condition
        with condition:
            ticket_issued = condition.wait_for(lambda: runtime.freshness._next_ticket >= 2, timeout=5)
        assert ticket_issued

        first_release.set()
        thread_a.join(timeout=5)
        thread_b.join(timeout=5)

        assert isinstance(errors.get("a"), _Boom)
        assert results["b"] == FreshnessScan()
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

        (tmp_path / "main.py").unlink()
        (tmp_path / "main.py").write_text("value = 1\n")
        runtime.ensure_fresh()

        (tmp_path / "pyrightconfig.json").write_text("{}\n")
        config_scan = runtime.ensure_fresh()

        assert config_scan.config_changed == ("pyrightconfig.json",)
        assert config_scan.reattributed == (LanguageFamily.PYTHON,)
    finally:
        runtime.stop()


@pytest.mark.parametrize("tracked", (True, False), ids=("tracked", "untracked"))
def test_same_stat_source_rewrite_is_reconciled_before_a_semantic_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tracked: bool
) -> None:
    _git_repository(tmp_path)
    target = tmp_path / "main.py"
    target.write_text("value = 1\n")
    if tracked:
        subprocess.run(["git", "add", "main.py"], cwd=tmp_path, check=True, capture_output=True)
    real_fstat = os.fstat
    real_stat = os.stat
    real_lstat = os.lstat
    monkeypatch.setattr(inventory_module.os, "fstat", lambda fd: _stat_with_fixed_times(real_fstat(fd)))
    monkeypatch.setattr(
        inventory_module.os,
        "stat",
        lambda path, *args, **kwargs: _stat_with_fixed_times(real_stat(path, *args, **kwargs)),
    )
    monkeypatch.setattr(
        inventory_module.os,
        "lstat",
        lambda path, *args, **kwargs: _stat_with_fixed_times(real_lstat(path, *args, **kwargs)),
    )
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    runtime = _git_runtime(tmp_path, adapters, contexts)
    try:
        runtime.load_document_symbols("main.py")
        python = adapters[LanguageFamily.PYTHON]
        python.client.notifications.clear()
        target.write_text("value = 2\n")

        scan = runtime.ensure_fresh()

        assert scan.changed == ("main.py",)
        notification = cast(Mapping[str, Any], python.client.notifications[-1][1])
        assert notification["contentChanges"] == [{"text": "value = 2\n"}]
    finally:
        runtime.stop()


def test_unstable_byte_observation_fails_before_freshness_state_is_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_repository(tmp_path)
    target = tmp_path / "main.py"
    file_size = 2 * 1024 * 1024
    prefix = b"value = 1\n#"
    target.write_bytes(prefix + b"x" * (file_size - len(prefix)))
    real_fstat = os.fstat
    real_stat = os.stat
    real_lstat = os.lstat
    real_read = os.read
    target_inode = target.stat().st_ino
    monkeypatch.setattr(inventory_module.os, "fstat", lambda fd: _stat_with_fixed_times(real_fstat(fd)))
    monkeypatch.setattr(
        inventory_module.os,
        "stat",
        lambda path, *args, **kwargs: _stat_with_fixed_times(real_stat(path, *args, **kwargs)),
    )
    monkeypatch.setattr(
        inventory_module.os,
        "lstat",
        lambda path, *args, **kwargs: _stat_with_fixed_times(real_lstat(path, *args, **kwargs)),
    )
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    runtime = _git_runtime(tmp_path, adapters, contexts)
    try:
        runtime.load_document_symbols("main.py")
        python = adapters[LanguageFamily.PYTHON]
        python.client.notifications.clear()
        inventory_before = runtime.inventory
        generations_before = python.snapshot().generations
        triggered = False

        def racing_read(file_descriptor: int, size: int) -> bytes:
            nonlocal triggered
            chunk = real_read(file_descriptor, size)
            if chunk and not triggered and real_fstat(file_descriptor).st_ino == target_inode:
                triggered = True
                changed_prefix = b"value = 2\n#"
                with target.open("r+b") as stream:
                    stream.write(changed_prefix + b"y" * (1024 * 1024 - len(changed_prefix)))
                    stream.flush()
                    os.fsync(stream.fileno())
            return chunk

        monkeypatch.setattr(inventory_module.os, "read", racing_read)

        with pytest.raises(WorkspaceRuntimeError) as caught:
            runtime.ensure_fresh()

        assert triggered
        assert caught.value.code is RuntimeErrorCode.NOT_READY
        assert runtime.inventory is inventory_before
        assert python.snapshot().generations == generations_before
        assert python.client.notifications == []

        recovered = runtime.ensure_fresh()
        assert recovered.changed == ("main.py",)
        assert [method for method, _ in python.client.notifications] == [
            "workspace/didChangeWatchedFiles",
            "textDocument/didChange",
        ]
    finally:
        runtime.stop()


def test_same_stat_native_config_rewrite_restarts_the_running_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_repository(tmp_path)
    (tmp_path / "main.py").write_text("value = 1\n")
    config = tmp_path / "pyrightconfig.json"
    config.write_text('{"x":1}\n')
    real_fstat = os.fstat
    real_stat = os.stat
    real_lstat = os.lstat
    monkeypatch.setattr(inventory_module.os, "fstat", lambda fd: _stat_with_fixed_times(real_fstat(fd)))
    monkeypatch.setattr(
        inventory_module.os,
        "stat",
        lambda path, *args, **kwargs: _stat_with_fixed_times(real_stat(path, *args, **kwargs)),
    )
    monkeypatch.setattr(
        inventory_module.os,
        "lstat",
        lambda path, *args, **kwargs: _stat_with_fixed_times(real_lstat(path, *args, **kwargs)),
    )
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    runtime = _git_runtime(tmp_path, adapters, contexts)
    try:
        runtime.load_document_symbols("main.py")
        original = adapters[LanguageFamily.PYTHON]
        config.write_text('{"y":2}\n')

        scan = runtime.ensure_fresh()

        assert scan.config_changed == ("pyrightconfig.json",)
        assert adapters[LanguageFamily.PYTHON] is not original
    finally:
        runtime.stop()


def test_native_config_deletion_is_a_stable_change_and_restarts_the_adapter(tmp_path: Path) -> None:
    _git_repository(tmp_path)
    (tmp_path / "main.py").write_text("value = 1\n")
    config = tmp_path / "pyrightconfig.json"
    config.write_text("{}\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    runtime = _git_runtime(tmp_path, adapters, contexts)
    try:
        runtime.load_document_symbols("main.py")
        original = adapters[LanguageFamily.PYTHON]
        config.unlink()

        scan = runtime.ensure_fresh()

        assert scan.config_changed == ("pyrightconfig.json",)
        assert adapters[LanguageFamily.PYTHON] is not original
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


def test_config_timeout_does_not_lose_same_scan_healthy_family_events(tmp_path: Path) -> None:
    _git_repository(tmp_path)
    (tmp_path / "main.py").write_text("value = 1\n")
    (tmp_path / "main.ts").write_text("export const value = 1;\n")
    (tmp_path / "gone.ts").write_text("export const gone = 1;\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    runtime = _git_runtime(tmp_path, adapters, contexts, future_timeout=0.05)
    release = threading.Event()
    entered = threading.Event()
    try:
        runtime.load_document_symbols("main.py")
        runtime.load_document_symbols("main.ts")
        original_python = adapters[LanguageFamily.PYTHON]
        typescript = adapters[LanguageFamily.TYPESCRIPT]
        typescript.client.notifications.clear()
        before = typescript.context.scope_tracker.generations

        def block_executor() -> None:
            entered.set()
            assert release.wait(5)

        blocked = runtime.executor.submit(block_executor)
        assert entered.wait(5)
        (tmp_path / "pyrightconfig.json").write_text('{"include": ["*.py"]}\n')
        (tmp_path / "main.ts").write_text("export const value = 22222;\n")
        (tmp_path / "created.ts").write_text("export const fresh = 1;\n")
        (tmp_path / "gone.ts").unlink()

        with pytest.raises(TimeoutError):
            runtime.ensure_fresh()

        after_failure = typescript.context.scope_tracker.generations
        assert after_failure.trust_inventory > before.trust_inventory
        assert after_failure.configured_program > before.configured_program
        assert after_failure.path_scoped["main.ts"] == before.path_scoped.get("main.ts", 0) + 1
        assert after_failure.path_scoped["created.ts"] == 1
        assert runtime.executor.snapshot().queue_size == 1
        unavailable = cast(Mapping[str, object], runtime.status()["unavailable_language_families"])
        python_error = cast(Mapping[str, object], cast(Mapping[str, object], unavailable["python"])["error"])
        assert python_error["code"] == "TIMED_OUT"
        assert runtime.get_symbols_overview("main.py").to_dict()["error"]["code"] == "TIMED_OUT"

        release.set()
        blocked.result(timeout=5)
        assert runtime.ensure_fresh().config_changed == ()
        runtime.executor.submit(lambda: None).result(timeout=5)

        assert adapters[LanguageFamily.PYTHON] is not original_python
        assert original_python.stop_calls == 1
        assert typescript.context.scope_tracker.generations == after_failure
        methods = [method for method, _ in typescript.client.notifications]
        assert methods == [
            "workspace/didChangeWatchedFiles",
            "textDocument/didChange",
            "textDocument/didOpen",
            "textDocument/didClose",
        ]
        changes = cast(Mapping[str, Any], typescript.client.notifications[0][1])["changes"]
        assert {(item["uri"].rsplit("/", 1)[-1], item["type"]) for item in changes} == {
            ("created.ts", 1),
            ("main.ts", 2),
            ("gone.ts", 3),
        }
        refreshed = cast(Mapping[str, Any], typescript.client.notifications[1][1])
        assert refreshed["textDocument"] == {"uri": (tmp_path / "main.ts").resolve().as_uri(), "version": 2}
        assert refreshed["contentChanges"] == [{"text": "export const value = 22222;\n"}]
        opened = cast(Mapping[str, Any], typescript.client.notifications[2][1])["textDocument"]
        assert opened["uri"] == (tmp_path / "created.ts").resolve().as_uri()
    finally:
        release.set()
        runtime.stop()


def test_freshness_refreshes_or_closes_already_open_documents_without_reopening_the_family(tmp_path: Path) -> None:
    _git_repository(tmp_path)
    (tmp_path / "changed.py").write_text("OldSymbol = 1\n")
    (tmp_path / "deleted.py").write_text("DeletedSymbol = 1\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    runtime = _git_runtime(tmp_path, adapters, contexts)
    try:
        runtime.load_document_symbols("changed.py")
        runtime.load_document_symbols("deleted.py")
        python = adapters[LanguageFamily.PYTHON]
        python.client.notifications.clear()

        (tmp_path / "changed.py").write_text("NewSymbol = 1\n")
        (tmp_path / "deleted.py").unlink()
        scan = runtime.ensure_fresh()
        runtime.executor.submit(lambda: None).result(timeout=5)

        assert scan.changed == ("changed.py",)
        assert scan.deleted == ("deleted.py",)
        methods = [method for method, _ in python.client.notifications]
        assert methods == ["workspace/didChangeWatchedFiles", "textDocument/didChange", "textDocument/didClose"]
        changed = cast(Mapping[str, Any], python.client.notifications[1][1])
        assert changed["textDocument"] == {
            "uri": (tmp_path / "changed.py").resolve().as_uri(),
            "version": 2,
        }
        assert changed["contentChanges"] == [{"text": "NewSymbol = 1\n"}]
        closed = cast(Mapping[str, Any], python.client.notifications[2][1])
        assert closed["textDocument"] == {"uri": (tmp_path / "deleted.py").resolve().as_uri()}
        assert "deleted.py" not in python._open_documents
    finally:
        runtime.stop()


def test_config_restart_and_runtime_stop_share_atomic_cleanup_ownership(tmp_path: Path) -> None:
    _git_repository(tmp_path)
    (tmp_path / "main.py").write_text("value = 1\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    runtime = _git_runtime(tmp_path, adapters, contexts, future_timeout=1.0)
    submit_entered = threading.Event()
    allow_submit = threading.Event()
    worker_entered = threading.Event()
    allow_worker = threading.Event()
    freshness_failures: list[BaseException] = []
    stop_failures: list[BaseException] = []
    try:
        runtime.load_document_symbols("main.py")
        original = adapters[LanguageFamily.PYTHON]

        def block_submit() -> None:
            submit_entered.set()
            assert allow_submit.wait(5)

        def block_worker() -> None:
            worker_entered.set()
            assert allow_worker.wait(5)

        original.before_stop_submit = block_submit
        original.before_stop_worker = block_worker
        (tmp_path / "pyrightconfig.json").write_text('{"include": ["*.py"]}\n')

        def refresh() -> None:
            try:
                runtime.ensure_fresh()
            except BaseException as error:
                freshness_failures.append(error)

        refreshing = threading.Thread(target=refresh)
        refreshing.start()
        assert submit_entered.wait(5)

        def stop_runtime() -> None:
            try:
                runtime.stop()
            except BaseException as error:
                stop_failures.append(error)

        stopping = threading.Thread(target=stop_runtime)
        stopping.start()
        assert stopping.is_alive()
        allow_submit.set()
        assert worker_entered.wait(5)
        deadline = time.monotonic() + 5
        stop_claimed = False
        while time.monotonic() < deadline:
            with runtime._state_lock:
                if runtime._stopping:
                    stop_claimed = True
                    break
            time.sleep(0.001)
        assert stop_claimed, "runtime stop did not claim lifecycle ownership"
        allow_worker.set()
        refreshing.join(timeout=5)
        stopping.join(timeout=5)

        assert freshness_failures == []
        assert stop_failures == []
        assert original.stop_calls == 1
        assert original.stop_thread is not None
        assert adapters[LanguageFamily.PYTHON] is original
        assert runtime.adapters == {}
        assert runtime._pending_restarts == {}
        assert runtime.status()["stopped"] is True
    finally:
        allow_submit.set()
        allow_worker.set()
        runtime.stop()


def test_config_restart_retries_failed_cleanup_future_on_next_scan(tmp_path: Path) -> None:
    _git_repository(tmp_path)
    (tmp_path / "main.py").write_text("value = 1\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    runtime = _git_runtime(tmp_path, adapters, contexts)
    try:
        runtime.load_document_symbols("main.py")
        original = adapters[LanguageFamily.PYTHON]
        attempts = 0

        def fail_worker_once() -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("cleanup worker failed")

        original.before_stop_worker = fail_worker_once
        (tmp_path / "pyrightconfig.json").write_text('{"include": ["*.py"]}\n')

        with pytest.raises(WorkspaceRuntimeError) as caught:
            runtime.ensure_fresh()
        assert caught.value.code is RuntimeErrorCode.UNSUPPORTED
        assert original.stop_calls == 1
        assert runtime.status()["stopped"] is False
        assert LanguageFamily.PYTHON not in runtime.adapters
        with pytest.raises(WorkspaceRuntimeError) as route_error:
            runtime.route("main.py")
        assert route_error.value.code is RuntimeErrorCode.UNSUPPORTED

        assert runtime.ensure_fresh().dirty is False
        assert original.stop_calls == 2
        assert adapters[LanguageFamily.PYTHON] is not original
        assert runtime.route("main.py") is adapters[LanguageFamily.PYTHON]
        assert runtime.status()["stopped"] is False
    finally:
        runtime.stop()


def test_runtime_stop_retries_cleanup_admission_before_publishing_stopped(tmp_path: Path) -> None:
    _git_repository(tmp_path)
    (tmp_path / "main.py").write_text("value = 1\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    runtime = _git_runtime(tmp_path, adapters, contexts)
    runtime.load_document_symbols("main.py")
    adapter = adapters[LanguageFamily.PYTHON]
    attempts = 0

    def reject_once() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("cleanup admission rejected")

    adapter.before_stop_submit = reject_once
    with pytest.raises(RuntimeError, match="cleanup admission rejected"):
        runtime.stop()
    assert runtime.status()["stopped"] is False
    assert runtime.status()["stopping"] is True
    with pytest.raises(WorkspaceRuntimeError) as caught:
        runtime.route("main.py")
    assert caught.value.code is RuntimeErrorCode.STOPPED

    runtime.stop()
    assert runtime.status()["stopped"] is True
    assert runtime.status()["stopping"] is True
    assert attempts == 2
    assert adapter.stop_thread is not None


def test_runtime_stop_retries_failed_cleanup_future_before_publishing_stopped(tmp_path: Path) -> None:
    _git_repository(tmp_path)
    (tmp_path / "main.py").write_text("value = 1\n")
    adapters: dict[LanguageFamily, _Adapter] = {}
    contexts: dict[LanguageFamily, AdapterBuildContext] = {}
    runtime = _git_runtime(tmp_path, adapters, contexts)
    runtime.load_document_symbols("main.py")
    adapter = adapters[LanguageFamily.PYTHON]
    attempts = 0

    def fail_worker_once() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("cleanup worker failed")

    adapter.before_stop_worker = fail_worker_once
    with pytest.raises(RuntimeError, match="cleanup worker failed"):
        runtime.stop()
    assert adapter.stop_calls == 1
    assert runtime.status()["stopped"] is False
    assert runtime.status()["stopping"] is True
    with pytest.raises(WorkspaceRuntimeError) as caught:
        runtime.route("main.py")
    assert caught.value.code is RuntimeErrorCode.STOPPED

    runtime.stop()
    assert adapter.stop_calls == 2
    assert runtime.status()["stopped"] is True
    assert runtime.status()["stopping"] is True


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
        assert [method for method, _ in python.client.notifications] == [
            "workspace/didChangeWatchedFiles",
            "textDocument/didChange",
        ]
    finally:
        runtime.stop()


def test_read_only_non_git_unstable_hash_fails_closed_and_recovers_without_a_full_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "main.py"
    file_size = 2 * 1024 * 1024
    prefix = b"value = 1\n#"
    source.write_bytes(prefix + b"x" * (file_size - len(prefix)))
    real_fstat = os.fstat
    real_stat = os.stat
    real_lstat = os.lstat
    real_read = os.read
    source_inode = source.stat().st_ino
    monkeypatch.setattr(inventory_module.os, "fstat", lambda fd: _stat_with_fixed_times(real_fstat(fd)))
    monkeypatch.setattr(
        inventory_module.os,
        "stat",
        lambda path, *args, **kwargs: _stat_with_fixed_times(real_stat(path, *args, **kwargs)),
    )
    monkeypatch.setattr(
        inventory_module.os,
        "lstat",
        lambda path, *args, **kwargs: _stat_with_fixed_times(real_lstat(path, *args, **kwargs)),
    )
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
        inventory_before = runtime.inventory
        states_before = dict(runtime.freshness._states)
        generations_before = python.snapshot().generations
        rebuilds_before = rebuilds
        triggered = False

        def racing_read(file_descriptor: int, size: int) -> bytes:
            nonlocal triggered
            chunk = real_read(file_descriptor, size)
            if chunk and not triggered and real_fstat(file_descriptor).st_ino == source_inode:
                triggered = True
                changed_prefix = b"value = 2\n#"
                with source.open("r+b") as stream:
                    stream.write(changed_prefix + b"y" * (1024 * 1024 - len(changed_prefix)))
                    stream.flush()
                    os.fsync(stream.fileno())
            return chunk

        monkeypatch.setattr(inventory_module.os, "read", racing_read)

        with pytest.raises(WorkspaceRuntimeError) as caught:
            runtime.freshness.ensure_path_fresh("main.py")

        assert triggered
        assert caught.value.code is RuntimeErrorCode.NOT_READY
        assert runtime.inventory is inventory_before
        assert runtime.freshness._states == states_before
        assert python.snapshot().generations == generations_before
        assert python.client.notifications == []
        assert rebuilds == rebuilds_before

        recovered = runtime.freshness.ensure_path_fresh("main.py")
        assert recovered.changed == ("main.py",)
        assert rebuilds == rebuilds_before
        assert [method for method, _ in python.client.notifications] == [
            "workspace/didChangeWatchedFiles",
            "textDocument/didChange",
        ]
    finally:
        runtime.stop()
