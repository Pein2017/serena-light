"""End-to-end freshness and guarded-edit behaviour on a real policy and runtime.

These cases exercise the whole workspace lane -- real `WorkspacePolicy`, real
`WorkspaceRuntime`, real Git inventory, real atomic writer -- with only the
language server itself replaced, so the guarded-edit decisions are made by the
production code paths rather than by test doubles.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import threading
from collections.abc import Callable, Mapping
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
from serena_light.lsp.positions import FileSnapshot, PositionEncoding
from serena_light.tools.editing import NotificationResult, ReplacementNotification
from serena_light.workspace.identity import PinnedMsRoots, WorkspacePolicy
from serena_light.workspace.runtime import AdapterBuildContext, AdapterFactory, WorkspaceRuntime
from serena_light.workspace.scope import (
    LanguageFamily,
    NativeProgramAttribution,
    ProjectKind,
    ScopeProjection,
)

_SOURCE = b"def target():\n    return 1\n"
_SYMBOLS: tuple[Mapping[str, Any], ...] = (
    {
        "name": "target",
        "kind": 12,
        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 1, "character": 12}},
        "selectionRange": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 10}},
    },
)


class _Client:
    def __init__(self, before_request: Callable[[], None] | None = None) -> None:
        self.before_request = before_request
        self.requests: list[str] = []
        self.notifications: list[str] = []

    def request(self, method: str, params: object = None, *, timeout: float | None = None) -> object:
        del params, timeout
        self.requests.append(method)
        if self.before_request is not None:
            self.before_request()
        return list(_SYMBOLS)

    def notify(self, method: str, params: object = None) -> None:
        del params
        self.notifications.append(method)

    def shutdown(self, *, timeout: float = 2.0) -> None:
        del timeout


class _Adapter:
    """A language server stand-in; every workspace decision stays in production code."""

    def __init__(self, context: AdapterBuildContext) -> None:
        self.context = context
        self.client = _Client()
        self.before_edit: Callable[[], None] | None = None
        self.document_generation = 0

    def routes(self, path: str | Path) -> bool:
        return PurePosixPath(str(path)).suffix.lower() in {".py", ".pyi"}

    def snapshot(self) -> AdapterSnapshot:
        raw = RawLspProviders(
            definition=True,
            implementation=False,
            references=True,
            document_symbols=True,
            workspace_symbols=True,
        )
        return AdapterSnapshot(
            self.context.family.value,
            AdapterPhase.READY,
            raw,
            DerivedToolAvailability.from_raw(raw),
            EngineMetadata("pyright", "1.0", Path("/owned/server"), Path("/owned/python")),
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
        def worker() -> Any:
            # Runs on the edit worker, after authorization and freshness but
            # before the transaction takes the workspace lock.
            if self.before_edit is not None:
                self.before_edit()
            return operation(self.client)

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
        return DocumentReadinessTarget(
            uri, relative_path, absolute_path, version, self.document_generation, 0
        )

    def notify_edit_with_client(
        self,
        client: _Client,
        target: DocumentReadinessTarget,
        notification: ReplacementNotification,
    ) -> NotificationResult:
        del notification
        client.notify("textDocument/didChange")
        self.document_generation += 1
        return NotificationResult("notified", self.document_generation)

    def stop(self) -> Future[AdapterSnapshot]:
        return self.context.executor.submit(self.snapshot)


def _projection(root: Path, paths: tuple[str, ...]) -> ScopeProjection:
    """Stub only native attribution; every trust and edit decision stays real."""

    del root
    return ScopeProjection.from_attribution(
        trust_inventory_paths=paths,
        attribution=NativeProgramAttribution(
            LanguageFamily.PYTHON, ProjectKind.WORKSPACE_DEFAULT, None, paths
        ),
    )


def _policy(tmp_path: Path) -> tuple[WorkspacePolicy, Path]:
    data_root = tmp_path / "data"
    data_root.mkdir()
    prefix = tmp_path / "ms"
    purelib = prefix / "lib" / "python3.12" / "site-packages"
    transformers = purelib / "transformers"
    transformers.mkdir(parents=True)
    interpreter = prefix / "bin" / "python"
    interpreter.parent.mkdir()
    interpreter.touch()
    return (
        WorkspacePolicy(
            ms_roots=PinnedMsRoots(
                interpreter=interpreter.resolve(),
                stdlib=purelib.parent.resolve(),
                purelib=purelib.resolve(),
                platlib=purelib.resolve(),
                conda_prefix=prefix.resolve(),
            ),
            allowed_non_git_root=transformers,
            data_root=data_root,
        ),
        data_root,
    )


def _repository(data_root: Path) -> Path:
    root = data_root / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    (root / ".gitignore").write_text("ignored.py\n")
    (root / "main.py").write_bytes(_SOURCE)
    (root / "ignored.py").write_text("secret = 1\n")
    return root


def _runtime(tmp_path: Path, *, future_timeout: float = 35.0) -> tuple[WorkspaceRuntime, _Adapter]:
    policy, data_root = _policy(tmp_path)
    root = _repository(data_root)
    adapters: list[_Adapter] = []

    def build(context: AdapterBuildContext) -> _Adapter:
        adapter = _Adapter(context)
        adapters.append(adapter)
        return adapter

    runtime = WorkspaceRuntime(
        policy.resolve_activation(root),
        path_policy=policy,
        attributors={LanguageFamily.PYTHON: _projection},
        adapter_factories={LanguageFamily.PYTHON: cast(AdapterFactory, build)},
        future_timeout=future_timeout,
    )
    return runtime, adapters[0]


def _replace(runtime: WorkspaceRuntime, expected: bytes = _SOURCE) -> Mapping[str, Any]:
    return cast(
        Mapping[str, Any],
        runtime.replace_symbol_body(
            "target", "main.py", "def target():\n    return 2", hashlib.sha256(expected).hexdigest()
        ).to_dict(),
    )


def test_tracked_path_swapped_for_an_in_root_ignored_symlink_fails_closed(tmp_path: Path) -> None:
    runtime, adapter = _runtime(tmp_path)
    root = runtime.identity.root
    ignored = root / "ignored.py"
    tracked = root / "main.py"

    def substitute() -> None:
        tracked.unlink()
        tracked.symlink_to(ignored)

    # The swap lands after the freshness scan and the first authorization, so the
    # under-lock component walk is the only thing that can still refuse it.
    adapter.before_edit = substitute
    try:
        result = _replace(runtime)

        assert result["error"]["code"] == "INVALID_PATH"
        assert ignored.read_text() == "secret = 1\n"
        assert tracked.is_symlink()
        assert adapter.client.requests == []
        assert not list(root.glob(".*.serena-light-*.tmp"))
    finally:
        runtime.stop()


def test_symlinked_inventory_entry_is_dropped_by_the_next_freshness_scan(tmp_path: Path) -> None:
    runtime, _adapter = _runtime(tmp_path)
    root = runtime.identity.root
    try:
        assert "main.py" in runtime.inventory.paths
        (root / "main.py").unlink()
        (root / "main.py").symlink_to(root / "ignored.py")

        scan = runtime.ensure_fresh()

        assert scan.symlinked == ("main.py",)
        assert "main.py" not in runtime.inventory.paths
        assert _replace(runtime)["error"]["code"] in {"OUT_OF_WORKSPACE", "INVALID_PATH"}
        assert (root / "ignored.py").read_text() == "secret = 1\n"
    finally:
        runtime.stop()


def test_edit_timing_out_while_queued_is_timed_out_and_never_writes(tmp_path: Path) -> None:
    runtime, adapter = _runtime(tmp_path, future_timeout=0.05)
    root = runtime.identity.root
    release = threading.Event()
    try:
        occupied = runtime.executor.submit(lambda: release.wait(5))
        result = _replace(runtime)

        assert result["error"]["code"] == "TIMED_OUT"
        assert result["error"]["details"]["commit_state"] == "queued"
        release.set()
        assert occupied.result(timeout=5) is True
        runtime.executor.submit(lambda: None).result(timeout=5)
        assert (root / "main.py").read_bytes() == _SOURCE
        assert adapter.client.requests == []
    finally:
        release.set()
        runtime.stop()


def test_edit_timing_out_while_running_is_uncertain(tmp_path: Path) -> None:
    runtime, adapter = _runtime(tmp_path, future_timeout=0.05)
    started = threading.Event()
    release = threading.Event()

    def block() -> None:
        started.set()
        assert release.wait(5)

    adapter.client.before_request = block
    try:
        result = _replace(runtime)

        assert started.is_set()
        assert result["error"]["code"] == "UNCERTAIN"
        assert result["error"]["details"]["commit_state"] == "running"
        assert result["error"]["details"]["requires_current_reread"] is True
    finally:
        release.set()
        runtime.stop()


def test_post_replace_directory_fsync_failure_is_uncertain_and_is_never_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _adapter = _runtime(tmp_path)
    root = runtime.identity.root
    real_fsync = os.fsync

    def fsync_failing_on_directories(file_descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
            raise OSError("directory flush failed")
        real_fsync(file_descriptor)

    monkeypatch.setattr(os, "fsync", fsync_failing_on_directories)
    try:
        result = _replace(runtime)
        installed = (root / "main.py").read_bytes()

        assert result["error"]["code"] == "UNCERTAIN"
        assert result["error"]["retry"] == {"retryable": False}
        assert result["error"]["details"]["uncertain_stage"] == "directory_fsync"
        assert result["error"]["details"]["current_hash"] == hashlib.sha256(installed).hexdigest()
        assert installed != _SOURCE
        assert not list(root.glob(".*.serena-light-*.tmp"))

        # The same expected hash must never be able to repeat the edit.
        monkeypatch.setattr(os, "fsync", real_fsync)
        assert _replace(runtime)["error"]["code"] == "STALE_HASH"
    finally:
        runtime.stop()


def test_freshness_runs_before_a_semantic_operation_on_the_real_git_root(tmp_path: Path) -> None:
    runtime, adapter = _runtime(tmp_path)
    root = runtime.identity.root
    try:
        runtime.get_symbols_overview("main.py")
        adapter.client.notifications.clear()
        (root / "created.py").write_text("fresh = 1\n")

        assert runtime.get_symbols_overview("main.py").to_dict()["ok"] is True
        runtime.executor.submit(lambda: None).result(timeout=5)

        assert "created.py" in runtime.inventory.paths
        assert adapter.client.notifications[:3] == [
            "workspace/didChangeWatchedFiles",
            "textDocument/didOpen",
            "textDocument/didClose",
        ]
        # status() reports the latest completed guarded scan.  A source-derived
        # read runs a clean postflight scan after the preflight above already
        # reconciled the new file, so the reported scan is that later, empty
        # pass; the preflight reconciliation itself is independently proven by
        # the inventory membership and notification assertions above.
        freshness = cast(Mapping[str, Any], cast(Mapping[str, Any], runtime.status())["freshness"])
        assert freshness["created"] == ()
        assert freshness["opened"] == ()
    finally:
        runtime.stop()


def test_replace_symbol_body_takes_exactly_one_preflight_scan_and_one_edit_submission_and_is_never_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guards the edit/read ownership split ahead of the ``_tool_envelope`` refactor.

    ``replace_symbol_body`` must run the Git freshness scan exactly once, as a
    preflight only; it must submit the edit transaction exactly once; and it
    must not gain a postflight scan or an automatic replay even when the
    workspace changes during the edit itself, since edits stay outside the
    fresh-read replay boundary.
    """
    runtime, adapter = _runtime(tmp_path)
    root = runtime.identity.root
    scan_calls = 0
    edit_submissions = 0
    real_ensure_fresh = runtime._freshness.ensure_fresh
    real_submit_edit = adapter.submit_edit

    def counting_ensure_fresh() -> Any:
        nonlocal scan_calls
        scan_calls += 1
        return real_ensure_fresh()

    def counting_submit_edit(operation: Callable[[_Client], Any]) -> Future[Any]:
        nonlocal edit_submissions
        edit_submissions += 1
        return real_submit_edit(operation)

    monkeypatch.setattr(runtime._freshness, "ensure_fresh", counting_ensure_fresh)
    monkeypatch.setattr(adapter, "submit_edit", counting_submit_edit)

    def disturb_during_edit() -> None:
        # This change lands after the one preflight scan has already run and
        # while the edit transaction itself is in flight.  A postflight scan or
        # an automatic replay would observe it; `replace_symbol_body` must not
        # take either action.
        (root / "created_during_edit.py").write_text("late = 1\n")

    adapter.before_edit = disturb_during_edit
    try:
        result = _replace(runtime)

        assert result["ok"] is True
        assert (root / "main.py").read_bytes() == b"def target():\n    return 2\n"
        assert scan_calls == 1
        assert edit_submissions == 1
    finally:
        runtime.stop()


def test_read_only_transformers_root_is_not_editable(tmp_path: Path) -> None:
    policy, _data_root = _policy(tmp_path)
    transformers = policy.allowed_non_git_root
    (transformers / "module.py").write_bytes(_SOURCE)
    adapters: list[_Adapter] = []

    def build(context: AdapterBuildContext) -> _Adapter:
        adapter = _Adapter(context)
        adapters.append(adapter)
        return adapter

    runtime = WorkspaceRuntime(
        policy.resolve_activation(transformers),
        path_policy=policy,
        attributors={LanguageFamily.PYTHON: _projection},
        adapter_factories={LanguageFamily.PYTHON: cast(AdapterFactory, build)},
    )
    try:
        result = cast(
            Mapping[str, Any],
            runtime.replace_symbol_body(
                "target", "module.py", "def target():\n    return 2", hashlib.sha256(_SOURCE).hexdigest()
            ).to_dict(),
        )

        assert result["error"]["code"] == "READ_ONLY_ROOT"
        assert (transformers / "module.py").read_bytes() == _SOURCE
    finally:
        runtime.stop()
