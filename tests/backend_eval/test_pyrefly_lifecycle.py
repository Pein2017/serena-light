"""Real locked-Pyrefly lifecycle gates for Phase 2 Task 7.

These tests exercise the immutable candidate runtime rather than an ambient
``pyrefly`` executable.  They deliberately keep every workspace disposable:
the evaluator must never create a Pyrefly configuration in an evaluated root.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import pytest

import scripts.backend_eval.protocol as protocol_module
from scripts.backend_eval.models import CandidateProtocolOutcome, ServiceConfigIdentity
from scripts.backend_eval.process import Deadline, monotonic_clock
from scripts.backend_eval.protocol import (
    BackendProtocolSpec,
    ProtocolSession,
    protocol_session_from_error,
    run_protocol_probe,
)
from scripts.backend_eval.pyrefly_probe import (
    pyrefly_protocol_spec,
    run_pyrefly_capability_probe,
)
from scripts.backend_eval.runtime import (
    BACKEND_ENVIRONMENT_KEYS,
    CandidateRuntime,
    load_prepared_candidate_runtime,
    minimal_backend_environment,
)
from serena_light.lsp.adapter import AdapterRuntime, RawLspProviders, SubprocessAdapterRuntimeProvider
from serena_light.lsp.client import CONTENT_MODIFIED, LspResponseError, LspTransportClosed, SyncLspClient

pytestmark = pytest.mark.timeout(180)

_RUNTIME_LOCK_DIGEST = "6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062"
_RUNTIME_MANIFEST_SHA256 = "e578bf4d6f1d98df96140d6c03b793a26af60658e49ea03b6810581898a6b4ec"
_RUNTIME_ROOT = Path("/data/CoordExp/.codex/runtime/serena-light/backend-eval") / _RUNTIME_LOCK_DIGEST
_MS_SWIFT = Path("/data/ms-swift")
_KNOWN_FILE = Path("swift/infer_engine/lmdeploy_engine.py")
_KNOWN_POSITION = (14, 25)
_REQUEST_CANCELLED = -32800
_REAL_DEADLINE_SECONDS = 90.0
_PROCESS_REAP_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class _StartedProcess:
    process: subprocess.Popen[bytes]
    process_group: int


@pytest.fixture(scope="module")
def locked_pyrefly_runtime() -> CandidateRuntime:
    """Read the exact Task 1.8 runtime without preparing or repairing it."""

    runtime = load_prepared_candidate_runtime(
        _RUNTIME_ROOT,
        expected_lock_digest=_RUNTIME_LOCK_DIGEST,
        expected_manifest_sha256=_RUNTIME_MANIFEST_SHA256,
    )
    assert runtime.pyrefly.is_file()
    return runtime


def _pyrefly_config(runtime: CandidateRuntime) -> object:
    return next(identity for identity in runtime.service_configs if identity.backend == "pyrefly")


def _workspace_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _service_config(runtime: CandidateRuntime) -> ServiceConfigIdentity:
    return cast(ServiceConfigIdentity, _pyrefly_config(runtime))


def _run_real_session[T](
    runtime: CandidateRuntime,
    workspace: Path,
    session: Callable[[SyncLspClient, RawLspProviders], T],
    *,
    deadline: Deadline | None = None,
    spec_transform: Callable[[BackendProtocolSpec], BackendProtocolSpec] | None = None,
) -> ProtocolSession[T]:
    spec = pyrefly_protocol_spec(runtime, _service_config(runtime))
    if spec_transform is not None:
        spec = spec_transform(spec)
    return run_protocol_probe(
        spec,
        runtime,
        workspace,
        deadline=deadline or Deadline.start(monotonic_clock, _REAL_DEADLINE_SECONDS),
        session=session,
    )


def _capture_started_processes(monkeypatch: pytest.MonkeyPatch) -> list[_StartedProcess]:
    captured: list[_StartedProcess] = []
    original_start = protocol_module.SubprocessAdapterRuntimeProvider.start

    def spy_start(
        self: SubprocessAdapterRuntimeProvider,
        *,
        notification_handler: Callable[[str, Any], None],
        terminal_handler: Callable[[BaseException], None],
    ) -> AdapterRuntime:
        adapter_runtime = original_start(
            self,
            notification_handler=notification_handler,
            terminal_handler=terminal_handler,
        )
        process = adapter_runtime.process
        assert process is not None
        captured.append(_StartedProcess(process, os.getpgid(process.pid)))
        return adapter_runtime

    monkeypatch.setattr(protocol_module.SubprocessAdapterRuntimeProvider, "start", spy_start)
    return captured


def _process_group_has_live_member(process_group: int) -> bool:
    process_root = Path("/proc")
    for entry in process_root.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            if os.getpgid(int(entry.name)) == process_group:
                state = (entry / "stat").read_text(encoding="utf-8", errors="replace").split()
                if len(state) >= 3 and state[2] != "Z":
                    return True
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return False


def _wait_until(predicate: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _assert_processes_reaped(captured: list[_StartedProcess]) -> None:
    assert len(captured) == 1
    started = captured[0]
    assert started.process.wait(timeout=_PROCESS_REAP_SECONDS) is not None
    assert _wait_until(
        lambda: not _process_group_has_live_member(started.process_group),
        _PROCESS_REAP_SECONDS,
    )


def _read_process_environment(process: subprocess.Popen[bytes]) -> dict[str, str]:
    payload = (Path("/proc") / str(process.pid) / "environ").read_bytes()
    result: dict[str, str] = {}
    for item in payload.split(b"\0"):
        if not item:
            continue
        key, separator, value = item.partition(b"=")
        assert separator == b"="
        result[os.fsdecode(key)] = os.fsdecode(value)
    return result


@pytest.fixture(scope="module")
def real_pyrefly_capability_outcome(locked_pyrefly_runtime: CandidateRuntime) -> CandidateProtocolOutcome:
    """One bounded, real fixed-corpus observation; a candidate failure is evidence, not a skip."""

    return run_pyrefly_capability_probe(
        locked_pyrefly_runtime,
        _MS_SWIFT,
        _KNOWN_FILE,
        _KNOWN_POSITION,
        deadline=Deadline.start(monotonic_clock, _REAL_DEADLINE_SECONDS, reserve=15.0),
    )


def test_real_pyrefly_refuses_hostile_initialize_without_service_owned_config_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    locked_pyrefly_runtime: CandidateRuntime,
) -> None:
    """Task 2.7: omission of configPath is a fail-closed pre-launch boundary.

    Starting Pyrefly without its external service-owned configuration is not a
    harmless alternate mode: upstream may discover, migrate, or create a
    workspace-local configuration.  The evaluator must reject it before an
    owned child gets the chance to touch the workspace.
    """

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text("value = 1\n", encoding="utf-8")
    before = _workspace_snapshot(workspace)
    spec = pyrefly_protocol_spec(locked_pyrefly_runtime, _service_config(locked_pyrefly_runtime))
    original_initialize = spec.initialize_params

    def hostile_initialize(root: Path) -> dict[str, object]:
        params = dict(original_initialize(root))
        options = dict(cast("dict[str, object]", params["initializationOptions"]))
        pyrefly_options = dict(cast("dict[str, object]", options["pyrefly"]))
        del pyrefly_options["configPath"]
        options["pyrefly"] = pyrefly_options
        params["initializationOptions"] = options
        return params

    hostile_spec: BackendProtocolSpec = replace(spec, initialize_params=hostile_initialize)
    started = _capture_started_processes(monkeypatch)

    with pytest.raises(ValueError, match="configPath"):
        run_protocol_probe(
            hostile_spec,
            locked_pyrefly_runtime,
            workspace,
            deadline=Deadline.start(monotonic_clock, 45.0),
            session=lambda _client, _providers: None,
        )

    assert started == []
    assert _workspace_snapshot(workspace) == before


@pytest.mark.external_repo(root=str(_MS_SWIFT), snapshot_env="SERENA_LIGHT_MS_SWIFT_SNAPSHOT")
def test_real_pyrefly_cold_readiness_is_bounded_and_records_diagnostics_mode(
    real_pyrefly_capability_outcome: CandidateProtocolOutcome,
) -> None:
    """A locked candidate's real failure remains typed evidence, never empty success."""

    outcome = real_pyrefly_capability_outcome
    invalid_advertised = tuple(
        capability
        for capability in outcome.capabilities
        if capability.advertised and capability.normalized_valid is not True
    )

    assert outcome.engine_version == "1.2.0"
    assert 0.0 < outcome.lifecycle.cold_readiness_seconds < _REAL_DEADLINE_SECONDS
    assert outcome.lifecycle.diagnostics_mode in {"push", "pull"}
    if outcome.lifecycle.diagnostics_mode == "pull":
        assert outcome.gate_disposition == "seam_incompatible_pull_only"
    else:
        assert outcome.gate_disposition == "fail"
        assert invalid_advertised
    assert outcome.lifecycle.shutdown_clean is True
    assert outcome.lifecycle.cleanup_clean is True


def test_real_pyrefly_sends_exact_service_owned_config_to_the_locked_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    locked_pyrefly_runtime: CandidateRuntime,
) -> None:
    """The actual initialize RPC must carry the exact external config, not only the spec literal."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text("value = 1\n", encoding="utf-8")
    before = _workspace_snapshot(workspace)
    observed: list[Mapping[str, object]] = []
    original_request = SyncLspClient.request

    def capture_initialize(
        self: SyncLspClient,
        method: str,
        params: Any = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        if method == "initialize":
            assert isinstance(params, Mapping)
            observed.append(cast(Mapping[str, object], params))
        return original_request(self, method, params, timeout=timeout)

    monkeypatch.setattr(SyncLspClient, "request", capture_initialize)
    protocol_session = _run_real_session(
        locked_pyrefly_runtime,
        workspace,
        lambda _client, _providers: "initialized",
    )

    config = _service_config(locked_pyrefly_runtime)
    assert len(observed) == 1
    options = cast(Mapping[str, object], observed[0]["initializationOptions"])
    pyrefly_options = cast(Mapping[str, object], options["pyrefly"])
    assert options["pythonPath"] == next(
        item.interpreter_path for item in locked_pyrefly_runtime.environments if item.name == "ms"
    )
    assert pyrefly_options == {
        "configPath": config.config_path,
        "diagnosticMode": "workspace",
    }
    assert Path(config.config_path).is_file()
    assert not Path(config.config_path).is_relative_to(workspace)
    assert protocol_session.result == "initialized"
    assert protocol_session.cleanup_errors == ()
    assert protocol_session.exit_status == 0
    assert _workspace_snapshot(workspace) == before


@pytest.mark.external_repo(root=str(_MS_SWIFT), snapshot_env="SERENA_LIGHT_MS_SWIFT_SNAPSHOT")
@pytest.mark.parametrize(
    ("response_code", "lifecycle_counter"),
    [
        (CONTENT_MODIFIED, "content_modified_count"),
        (_REQUEST_CANCELLED, "request_cancelled_count"),
    ],
)
def test_real_pyrefly_observes_each_retryable_error_once_without_hidden_retry(
    locked_pyrefly_runtime: CandidateRuntime,
    monkeypatch: pytest.MonkeyPatch,
    response_code: int,
    lifecycle_counter: str,
) -> None:
    """Inject only one real-client response after real initialize and preserve all cleanup."""

    original_request = SyncLspClient.request
    attempts = 0

    def inject_references_error(
        self: SyncLspClient,
        method: str,
        params: Any = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        nonlocal attempts
        if method == "textDocument/references":
            attempts += 1
            assert self._retry_methods == frozenset()
            raise LspResponseError(response_code, "injected lifecycle response")
        return original_request(self, method, params, timeout=timeout)

    monkeypatch.setattr(SyncLspClient, "request", inject_references_error)
    outcome = run_pyrefly_capability_probe(
        locked_pyrefly_runtime,
        _MS_SWIFT,
        _KNOWN_FILE,
        _KNOWN_POSITION,
        deadline=Deadline.start(monotonic_clock, _REAL_DEADLINE_SECONDS, reserve=15.0),
    )
    references = next(item for item in outcome.capabilities if item.name == "references")

    assert attempts == 1
    assert references.accepted is False
    assert references.normalized_valid is False
    assert str(response_code) in references.notes
    # This proves the transport does not replay the injected request.  A real
    # Pyrefly run may independently report the same lifecycle code from another
    # capability, so the aggregate evidence is deliberately lower-bounded.
    assert getattr(outcome.lifecycle, lifecycle_counter) >= 1
    assert outcome.lifecycle.retry_seam_disabled is True
    # Error-path cleanup may need to terminate a server that did not honour the
    # normal shutdown grace.  The separate graceful-shutdown scenario covers
    # natural exit; this one must only prove that no process survives.
    assert outcome.lifecycle.cleanup_clean is True


def test_real_pyrefly_bounded_request_timeout_is_typed_and_reaped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    locked_pyrefly_runtime: CandidateRuntime,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured = _capture_started_processes(monkeypatch)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> object:
        return client.request("workspace/symbol", {"query": "never-wait"}, timeout=1e-9)

    with pytest.raises(TimeoutError, match="LSP request") as raised:
        _run_real_session(locked_pyrefly_runtime, workspace, session)

    evidence = protocol_session_from_error(raised.value)
    assert evidence is not None
    assert evidence.result is None
    assert evidence.cleanup_errors == ()
    _assert_processes_reaped(captured)


def test_real_pyrefly_crash_is_detected_and_its_process_group_is_reaped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    locked_pyrefly_runtime: CandidateRuntime,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured = _capture_started_processes(monkeypatch)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> None:
        assert len(captured) == 1
        process = captured[0].process
        process.kill()
        process.wait(timeout=_PROCESS_REAP_SECONDS)
        assert _wait_until(lambda: not client.is_running, _PROCESS_REAP_SECONDS)
        client.request("workspace/symbol", {"query": "after-crash"}, timeout=5.0)

    with pytest.raises(LspTransportClosed) as raised:
        _run_real_session(locked_pyrefly_runtime, workspace, session)

    evidence = protocol_session_from_error(raised.value)
    assert evidence is not None
    assert evidence.result is None
    assert evidence.exit_status == -signal.SIGKILL
    assert evidence.terminal_errors
    assert evidence.cleanup_errors == ()
    _assert_processes_reaped(captured)


def test_real_pyrefly_graceful_shutdown_leaves_no_parent_or_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    locked_pyrefly_runtime: CandidateRuntime,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    captured = _capture_started_processes(monkeypatch)

    protocol_session = _run_real_session(
        locked_pyrefly_runtime,
        workspace,
        lambda _client, _providers: "initialized",
    )

    assert protocol_session.result == "initialized"
    assert protocol_session.terminal_errors == ()
    assert protocol_session.cleanup_errors == ()
    assert protocol_session.exit_status == 0
    _assert_processes_reaped(captured)


def test_real_pyrefly_child_rejects_poisoned_proxy_and_ambient_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    locked_pyrefly_runtime: CandidateRuntime,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    poison = {
        "HTTP_PROXY": "http://127.0.0.1:9/forbidden-http",
        "https_proxy": "http://127.0.0.1:9/forbidden-https",
        "ALL_PROXY": "socks5://127.0.0.1:9/forbidden-all",
        "no_proxy": "attacker.invalid",
        "PYTHONHOME": "/tmp/forbidden-python-home",
        "CONDA_PREFIX": "/tmp/forbidden-conda",
        "PIP_INDEX_URL": "https://forbidden.invalid/simple",
        "UV_INDEX_URL": "https://forbidden.invalid/simple",
        "SERENA_LIGHT_TEST_SECRET": "must-not-reach-locked-pyrefly",
    }
    for key, value in poison.items():
        monkeypatch.setenv(key, value)
    captured = _capture_started_processes(monkeypatch)

    def session(_client: SyncLspClient, _providers: RawLspProviders) -> dict[str, str]:
        assert len(captured) == 1
        return _read_process_environment(captured[0].process)

    protocol_session = _run_real_session(locked_pyrefly_runtime, workspace, session)
    observed = protocol_session.result
    session_interpreter = protocol_session.engine.interpreter
    assert session_interpreter is not None
    expected = minimal_backend_environment(locked_pyrefly_runtime, session_interpreter)

    assert locked_pyrefly_runtime.root.is_relative_to(_RUNTIME_ROOT.parent)
    assert all(key not in observed for key in poison)
    assert not any(key.upper().endswith("_PROXY") for key in observed)
    assert {key: observed[key] for key in BACKEND_ENVIRONMENT_KEYS} == expected
    assert set(observed) - {"PWD", "LC_CTYPE"} == set(BACKEND_ENVIRONMENT_KEYS)
    _assert_processes_reaped(captured)


def test_real_pyrefly_stderr_secret_evidence_is_redacted_and_bounded(
    tmp_path: Path,
    locked_pyrefly_runtime: CandidateRuntime,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bearer = "pyrefly-lifecycle-fake-bearer-4af2"
    password = "pyrefly-lifecycle-fake-password-9c71"

    def add_stderr_fault(spec: BackendProtocolSpec) -> BackendProtocolSpec:
        def build_command(runtime: CandidateRuntime) -> tuple[str, ...]:
            script = (
                "printf '%s\\n' "
                f"'Authorization: Bearer {bearer}' "
                f"'password={password}' >&2; "
                'exec "$1" lsp --indexing-mode lazy-blocking --threads 1 --workspace-indexing-limit 2000'
            )
            return ("/bin/sh", "-c", script, "pyrefly-lifecycle-redaction-wrapper", str(runtime.pyrefly))

        return replace(spec, build_command=build_command)

    protocol_session = _run_real_session(
        locked_pyrefly_runtime,
        workspace,
        lambda _client, _providers: None,
        spec_transform=add_stderr_fault,
    )

    assert bearer not in protocol_session.stderr_tail
    assert password not in protocol_session.stderr_tail
    assert protocol_session.stderr_tail.count("<redacted>") >= 2
    assert len(protocol_session.stderr_tail) <= 1024
    assert protocol_session.cleanup_errors == ()
    assert protocol_session.exit_status == 0
