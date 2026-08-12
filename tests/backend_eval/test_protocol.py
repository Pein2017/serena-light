"""Interface, shared-runner, and source-ownership tests for the Phase 2 protocol plane.

``run_protocol_probe`` is exercised against a real subprocess that is a small fake LSP
server script, never a candidate backend (Pyright/ty/Pyrefly) -- proving the shared
transport/deadline/cleanup/environment/evidence discipline without launching anything this
task is not allowed to launch.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from scripts.backend_eval import protocol as protocol_module
from scripts.backend_eval.models import DIAGNOSTICS_MODES, EnvironmentIdentity, ServiceConfigIdentity
from scripts.backend_eval.process import Deadline, DeadlineExceeded, monotonic_clock
from scripts.backend_eval.protocol import BackendProtocolSpec, protocol_session_from_error, run_protocol_probe
from scripts.backend_eval.runtime import BACKEND_ENVIRONMENT_KEYS, SERVICE_CONFIG_RELPATHS, CandidateRuntime
from serena_light.lsp.adapter import (
    AdapterRuntime,
    EngineMetadata,
    RawLspProviders,
    SubprocessAdapterRuntimeProvider,
)
from serena_light.lsp.client import LspTransportClosed, SyncLspClient
from serena_light.lsp.positions import PositionEncoding

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INTERPRETER_VERSION = "3.12.11"

_FAKE_SERVER_TEMPLATE = r"""
import json
import os
import sys

CAPABILITIES = json.loads({capabilities_json!r})
ECHO_ENVIRONMENT = {echo_environment!r}
BAD_INITIALIZE_RESULT = {bad_initialize_result!r}
CRASH_ON_METHOD = json.loads({crash_on_method_json!r})
EXIT_STATUS_ON_SHUTDOWN = {exit_status_on_shutdown!r}
STDERR_TEXT = json.loads({stderr_text_json!r})

if STDERR_TEXT is not None:
    sys.stderr.write(STDERR_TEXT)
    sys.stderr.flush()


def _read(stream):
    length = None
    while True:
        line = stream.readline()
        if not line:
            raise SystemExit(0)
        if line in (b"\r\n", b"\n"):
            break
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    body = stream.read(length)
    return json.loads(body)


def _write(stream, payload):
    body = json.dumps(payload).encode()
    stream.write(("Content-Length: %d\r\n\r\n" % len(body)).encode())
    stream.write(body)
    stream.flush()


stdin = sys.stdin.buffer
stdout = sys.stdout.buffer
while True:
    message = _read(stdin)
    method = message.get("method")
    if CRASH_ON_METHOD is not None and method == CRASH_ON_METHOD:
        os._exit(1)
    if method == "initialize":
        result = [] if BAD_INITIALIZE_RESULT else {{"capabilities": CAPABILITIES}}
        _write(stdout, {{"jsonrpc": "2.0", "id": message["id"], "result": result}})
    elif method == "initialized":
        continue
    elif method == "shutdown":
        if EXIT_STATUS_ON_SHUTDOWN is not None:
            os._exit(EXIT_STATUS_ON_SHUTDOWN)
        _write(stdout, {{"jsonrpc": "2.0", "id": message["id"], "result": None}})
    elif method == "exit":
        break
    elif "id" in message:
        payload = {{"echoed": method}}
        if ECHO_ENVIRONMENT:
            payload["environment"] = dict(os.environ)
        _write(stdout, {{"jsonrpc": "2.0", "id": message["id"], "result": payload}})
"""


def _fake_server_script(
    *,
    capabilities: Mapping[str, object] | None = None,
    echo_environment: bool = False,
    bad_initialize_result: bool = False,
    crash_on_method: str | None = None,
    exit_status_on_shutdown: int | None = None,
    stderr_text: str | None = None,
) -> str:
    """A small fake stdio LSP server script -- never a real candidate backend."""

    resolved = {"definitionProvider": True, "referencesProvider": True} if capabilities is None else capabilities
    return _FAKE_SERVER_TEMPLATE.format(
        capabilities_json=json.dumps(dict(resolved)),
        echo_environment=echo_environment,
        bad_initialize_result=bad_initialize_result,
        crash_on_method_json=json.dumps(crash_on_method),
        exit_status_on_shutdown=exit_status_on_shutdown,
        stderr_text_json=json.dumps(stderr_text),
    )


def _fake_runtime(tmp_path: Path) -> CandidateRuntime:
    digest = "1" * 64
    root = tmp_path / digest
    venv_bin = root / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    python_path = venv_bin / "python"
    python_path.symlink_to(sys.executable)
    (venv_bin / "ty").write_bytes(b"")
    (venv_bin / "pyrefly").write_bytes(b"")
    home, cache, config = root / "home", root / "cache", root / "config"
    for directory in (home, cache, config):
        directory.mkdir()
    manifest_path = root / "runtime-manifest.json"
    manifest_path.write_text("{}")
    return CandidateRuntime(
        root=root,
        python=python_path,
        ty=venv_bin / "ty",
        pyrefly=venv_bin / "pyrefly",
        lock_digest=digest,
        executable_hashes=(("pyrefly", "2" * 64), ("ty", "3" * 64)),
        home=home,
        cache=cache,
        config=config,
        manifest_path=manifest_path,
        manifest_sha256="4" * 64,
        environments=(
            EnvironmentIdentity(
                name="llm-framework-study",
                interpreter_path=str(python_path),
                interpreter_realpath=str(python_path),
                version=_INTERPRETER_VERSION,
            ),
            EnvironmentIdentity(
                name="ms",
                interpreter_path=str(python_path),
                interpreter_realpath=str(python_path),
                version=_INTERPRETER_VERSION,
            ),
        ),
        service_configs=tuple(
            ServiceConfigIdentity(
                backend=backend,
                config_path=str(config / relpath),
                config_sha256="5" * 64,
                home_path=str(home),
                cache_path=str(cache),
            )
            for backend, relpath in sorted(SERVICE_CONFIG_RELPATHS.items())
        ),
    )


def _fake_spec(script: str | None = None) -> BackendProtocolSpec:
    resolved_script = _fake_server_script() if script is None else script
    return BackendProtocolSpec(
        name="fake",
        build_command=lambda runtime: (str(runtime.python), "-c", resolved_script),
        initialize_params=lambda root: {"rootUri": root.as_uri(), "capabilities": {}},
        request_handlers=None,
        engine=lambda runtime: EngineMetadata(name="fake", version="0.0.0", executable=runtime.python),
        position_encoding=PositionEncoding.UTF16,
        diagnostics_mode="push",
    )


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, delta: float) -> None:
        self._now += delta


# --- BackendProtocolSpec --------------------------------------------------------


def test_build_command_receives_the_prepared_runtime(tmp_path: Path) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = BackendProtocolSpec(
        name="pyright",
        build_command=lambda runtime: (str(runtime.python), "--version"),
        initialize_params=lambda root: {"rootUri": root.as_uri()},
        request_handlers=None,
        engine=lambda runtime: EngineMetadata(name="pyright", version="1.1.403", executable=runtime.python),
        position_encoding=PositionEncoding.UTF16,
        diagnostics_mode="push",
    )

    assert spec.build_command(runtime) == (str(runtime.python), "--version")


def test_backend_protocol_spec_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="name"):
        BackendProtocolSpec(
            name="",
            build_command=lambda runtime: (str(runtime.python),),
            initialize_params=lambda root: {},
            request_handlers=None,
            engine=lambda runtime: EngineMetadata(name="x", version="0", executable=runtime.python),
            position_encoding=PositionEncoding.UTF16,
            diagnostics_mode="push",
        )


def test_backend_protocol_spec_rejects_unknown_diagnostics_mode() -> None:
    with pytest.raises(ValueError, match="diagnostics_mode"):
        BackendProtocolSpec(
            name="pyright",
            build_command=lambda runtime: (str(runtime.python),),
            initialize_params=lambda root: {},
            request_handlers=None,
            engine=lambda runtime: EngineMetadata(name="x", version="0", executable=runtime.python),
            position_encoding=PositionEncoding.UTF16,
            diagnostics_mode="poll",
        )


def test_backend_protocol_spec_diagnostics_mode_shares_the_one_models_constant() -> None:
    """M9: one diagnostics-mode source -- BackendProtocolSpec accepts exactly the modes
    LifecycleEvidence (models.py) also validates against, not a second locally duplicated set."""

    assert frozenset({"push", "pull"}) == DIAGNOSTICS_MODES
    for mode in DIAGNOSTICS_MODES:
        spec = BackendProtocolSpec(
            name="pyright",
            build_command=lambda runtime: (str(runtime.python),),
            initialize_params=lambda root: {},
            request_handlers=None,
            engine=lambda runtime: EngineMetadata(name="x", version="0", executable=runtime.python),
            position_encoding=PositionEncoding.UTF16,
            diagnostics_mode=mode,
        )
        assert spec.diagnostics_mode == mode


# --- run_protocol_probe: real fake-process lifecycle ----------------------------


def test_run_protocol_probe_initializes_and_runs_session_then_stops(tmp_path: Path) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec()
    deadline = Deadline.start(monotonic_clock, 30.0)
    seen_providers: list[RawLspProviders] = []

    def session(client: SyncLspClient, providers: RawLspProviders) -> str:
        seen_providers.append(providers)
        result = client.request("textDocument/hover", {})
        assert isinstance(result, dict)
        return str(result["echoed"])

    session_result = run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    assert session_result.result == "textDocument/hover"
    assert session_result.raw_providers.definition is True
    assert session_result.raw_providers.references is True
    assert session_result.raw_providers.implementation is False
    assert seen_providers == [session_result.raw_providers]
    assert seen_providers[0] is session_result.raw_providers
    assert session_result.diagnostic_provider is False
    assert session_result.position_encoding == PositionEncoding.UTF16
    assert session_result.engine.name == "fake"
    assert session_result.terminal_errors == ()
    assert session_result.cleanup_errors == ()
    assert session_result.exit_status == 0
    assert isinstance(session_result.stderr_tail, str)


def test_run_protocol_probe_records_advertised_pull_diagnostic_provider(tmp_path: Path) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec(_fake_server_script(capabilities={"diagnosticProvider": {"interFileDependencies": True}}))
    deadline = Deadline.start(monotonic_clock, 30.0)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> None:
        del client

    session_result = run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    assert session_result.diagnostic_provider is True


def test_run_protocol_probe_negotiates_the_servers_selected_position_encoding(tmp_path: Path) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec(_fake_server_script(capabilities={"positionEncoding": "utf-32"}))
    deadline = Deadline.start(monotonic_clock, 30.0)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> None:
        del client

    session_result = run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    assert session_result.position_encoding == PositionEncoding.UTF32


def test_run_protocol_probe_rejects_a_non_mapping_initialize_result(tmp_path: Path) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec(_fake_server_script(bad_initialize_result=True))
    deadline = Deadline.start(monotonic_clock, 30.0)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> None:
        del client
        raise AssertionError("session must never run when initialize itself is malformed")

    with pytest.raises(TypeError, match="initialize result must be an object"):
        run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)


# --- Fix round 3: engine frozen before any process side effect -------------------


def test_run_protocol_probe_starts_no_child_process_when_the_engine_callable_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``spec.engine(runtime)`` is evaluated before ``provider.start()``, so a failing
    engine callable never launches a child and leaves no evidence to corrupt."""

    runtime = _fake_runtime(tmp_path)
    start_calls: list[SubprocessAdapterRuntimeProvider] = []
    original_start = protocol_module.SubprocessAdapterRuntimeProvider.start

    def spy_start(
        self: SubprocessAdapterRuntimeProvider,
        *,
        notification_handler: Callable[[str, Any], None],
        terminal_handler: Callable[[BaseException], None],
    ) -> AdapterRuntime:
        start_calls.append(self)
        return original_start(self, notification_handler=notification_handler, terminal_handler=terminal_handler)

    monkeypatch.setattr(protocol_module.SubprocessAdapterRuntimeProvider, "start", spy_start)

    def failing_engine(runtime: CandidateRuntime) -> EngineMetadata:
        del runtime
        raise RuntimeError("engine metadata unavailable")

    spec = BackendProtocolSpec(
        name="fake",
        build_command=lambda runtime: (str(runtime.python), "-c", _fake_server_script()),
        initialize_params=lambda root: {"rootUri": root.as_uri(), "capabilities": {}},
        request_handlers=None,
        engine=failing_engine,
        position_encoding=PositionEncoding.UTF16,
        diagnostics_mode="push",
    )
    deadline = Deadline.start(monotonic_clock, 30.0)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> None:
        del client
        raise AssertionError("session must never run when the engine callable itself failed")

    with pytest.raises(RuntimeError, match="engine metadata unavailable") as excinfo:
        run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    assert start_calls == []
    assert protocol_session_from_error(excinfo.value) is None


def test_run_protocol_probe_preserves_the_primary_exception_when_evidence_attachment_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``setattr`` failure while attaching evidence must never replace the original,
    already-in-flight primary exception's type or message."""

    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec()
    deadline = Deadline.start(monotonic_clock, 30.0)

    def failing_attach(error: BaseException, session_evidence: object) -> None:
        del session_evidence
        raise AttributeError("this exception type refuses new attributes")

    monkeypatch.setattr(protocol_module, "_attach_protocol_session", failing_attach)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> None:
        del client
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom") as excinfo:
        run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    assert type(excinfo.value) is RuntimeError
    assert str(excinfo.value) == "boom"
    notes = getattr(excinfo.value, "__notes__", ())
    assert any("could not attach protocol session evidence" in note for note in notes)


# --- Minor 5: pinned seam behavior of the reused private production helpers -----


def test_run_protocol_probe_treats_a_bare_true_diagnostic_provider_as_enabled(tmp_path: Path) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec(_fake_server_script(capabilities={"diagnosticProvider": True}))
    deadline = Deadline.start(monotonic_clock, 30.0)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> None:
        del client

    session_result = run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    assert session_result.diagnostic_provider is True


def test_run_protocol_probe_treats_an_absent_diagnostic_provider_as_disabled(tmp_path: Path) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec(_fake_server_script(capabilities={}))
    deadline = Deadline.start(monotonic_clock, 30.0)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> None:
        del client

    session_result = run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    assert session_result.diagnostic_provider is False


def test_run_protocol_probe_rejects_an_unsupported_selected_position_encoding(tmp_path: Path) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec(_fake_server_script(capabilities={"positionEncoding": "utf-7"}))
    deadline = Deadline.start(monotonic_clock, 30.0)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> None:
        del client
        raise AssertionError("session must never run when the server selects an unsupported encoding")

    with pytest.raises(ValueError, match="unsupported position encoding"):
        run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)


# --- Minor 6: stderr redaction and bound -----------------------------------------


def test_run_protocol_probe_redacts_bearer_tokens_and_secret_assignments_in_stderr_tail(tmp_path: Path) -> None:
    """Reuses serena_light.debug_logging's own redaction; asserts on its actual behavior
    (secret values gone, "<redacted>" present) rather than a re-derived expected format."""

    runtime = _fake_runtime(tmp_path)
    secret_text = (
        "Authorization: Bearer sk-not-a-real-secret-abc123\n"
        "Bearer plain-standalone-token-xyz\n"
        "password=hunter2-example\n"
    )
    spec = _fake_spec(_fake_server_script(capabilities={}, stderr_text=secret_text))
    deadline = Deadline.start(monotonic_clock, 30.0)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> None:
        del client

    session_result = run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    assert "sk-not-a-real-secret-abc123" not in session_result.stderr_tail
    assert "plain-standalone-token-xyz" not in session_result.stderr_tail
    assert "hunter2-example" not in session_result.stderr_tail
    assert "Bearer <redacted>" in session_result.stderr_tail
    assert session_result.stderr_tail.count("<redacted>") >= 3


def test_run_protocol_probe_bounds_the_stderr_tail_to_1024_characters(tmp_path: Path) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec(_fake_server_script(capabilities={}, stderr_text="z" * 5000))
    deadline = Deadline.start(monotonic_clock, 30.0)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> None:
        del client

    session_result = run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    assert len(session_result.stderr_tail) <= 1024
    assert session_result.stderr_tail.endswith("z" * 100)


# --- Important 1: failure evidence, same shape as success, original exception kept ----


def test_run_protocol_probe_attaches_protocol_session_evidence_when_session_raises(tmp_path: Path) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec()
    deadline = Deadline.start(monotonic_clock, 30.0)

    received_providers: RawLspProviders | None = None

    def session(client: SyncLspClient, providers: RawLspProviders) -> None:
        nonlocal received_providers
        del client
        received_providers = providers
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom") as excinfo:
        run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    evidence = protocol_session_from_error(excinfo.value)
    assert evidence is not None
    assert evidence.result is None
    # initialize already completed before session raised, so this is real, not default, evidence.
    assert evidence.raw_providers.definition is True
    assert received_providers is evidence.raw_providers
    assert evidence.engine.name == "fake"


def test_run_protocol_probe_attaches_no_evidence_when_the_deadline_is_already_expired_at_entry(
    tmp_path: Path,
) -> None:
    """The very first deadline check runs before ``spec.engine(runtime)`` is even called
    (round 3: engine is frozen right after it, before any process side effect), so at this
    earliest possible failure point there is no engine metadata to build evidence around --
    nothing happened yet, so nothing is attached, rather than fabricating a placeholder."""

    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec()
    clock = _FakeClock()
    deadline = Deadline.start(clock, 1.0)
    clock.advance(2.0)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> None:
        del client
        raise AssertionError("session must never run when the deadline is already expired")

    with pytest.raises(DeadlineExceeded) as excinfo:
        run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    assert protocol_session_from_error(excinfo.value) is None


def test_run_protocol_probe_attaches_protocol_session_evidence_when_session_overruns_the_deadline(
    tmp_path: Path,
) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec()
    clock = _FakeClock()
    deadline = Deadline.start(clock, 5.0)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> str:
        result = client.request("textDocument/hover", {})
        assert isinstance(result, dict)
        clock.advance(10.0)
        return str(result["echoed"])

    with pytest.raises(DeadlineExceeded) as excinfo:
        run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    evidence = protocol_session_from_error(excinfo.value)
    assert evidence is not None
    assert evidence.result is None
    assert evidence.raw_providers.definition is True


def test_run_protocol_probe_attaches_evidence_with_nonempty_terminal_errors_when_the_process_crashes(
    tmp_path: Path,
) -> None:
    """Important 1's third required scenario (terminal process error) and Minor 8 together."""

    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec(_fake_server_script(crash_on_method="textDocument/hover"))
    deadline = Deadline.start(monotonic_clock, 30.0)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> None:
        client.request("textDocument/hover", {})

    with pytest.raises(LspTransportClosed) as excinfo:
        run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    evidence = protocol_session_from_error(excinfo.value)
    assert evidence is not None
    assert evidence.result is None
    assert evidence.terminal_errors != ()
    assert evidence.cleanup_errors == ()
    assert evidence.exit_status == 1


def test_run_protocol_probe_reports_a_nonzero_exit_status_when_shutdown_crashes(
    tmp_path: Path,
) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec(_fake_server_script(exit_status_on_shutdown=23))
    deadline = Deadline.start(monotonic_clock, 30.0)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> str:
        del client
        return "session-complete"

    session_result = run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    assert session_result.result == "session-complete"
    assert session_result.exit_status == 23


def test_protocol_session_from_error_returns_none_for_an_unrelated_exception() -> None:
    assert protocol_session_from_error(RuntimeError("unrelated")) is None


# --- Important 1 continued: capture stderr/terminal_errors only after cleanup --------


def test_run_protocol_probe_stderr_tail_reflects_output_written_up_to_process_exit(tmp_path: Path) -> None:
    """The captured tail must include stderr the candidate writes while shutting down, not
    only whatever was written before ``session`` returned -- proving capture happens after
    cleanup, not before it."""

    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec(_fake_server_script(capabilities={}, stderr_text="startup-marker\n"))
    deadline = Deadline.start(monotonic_clock, 30.0)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> None:
        del client

    session_result = run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    assert "startup-marker" in session_result.stderr_tail


# --- Important 2: cleanup exception precedence ------------------------------------


def test_run_protocol_probe_calls_shutdown_and_stop_even_when_session_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec()
    deadline = Deadline.start(monotonic_clock, 30.0)
    stop_calls: list[AdapterRuntime] = []
    original_stop = protocol_module.SubprocessAdapterRuntimeProvider.stop

    def spy_stop(self: SubprocessAdapterRuntimeProvider, adapter_runtime: AdapterRuntime) -> None:
        stop_calls.append(adapter_runtime)
        original_stop(self, adapter_runtime)

    monkeypatch.setattr(protocol_module.SubprocessAdapterRuntimeProvider, "stop", spy_stop)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> None:
        del client
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    assert len(stop_calls) == 1


def test_run_protocol_probe_propagates_a_provider_stop_failure_when_there_is_no_primary_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``stop()`` failure is real evidence and must not be silently dropped when nothing else failed."""

    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec()
    deadline = Deadline.start(monotonic_clock, 30.0)
    original_stop = protocol_module.SubprocessAdapterRuntimeProvider.stop

    def failing_stop(self: SubprocessAdapterRuntimeProvider, adapter_runtime: AdapterRuntime) -> None:
        original_stop(self, adapter_runtime)  # still reap the real process
        raise TimeoutError("stop failed")

    monkeypatch.setattr(protocol_module.SubprocessAdapterRuntimeProvider, "stop", failing_stop)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> str:
        result = client.request("textDocument/hover", {})
        assert isinstance(result, dict)
        return str(result["echoed"])

    with pytest.raises(TimeoutError, match="stop failed") as excinfo:
        run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    evidence = protocol_session_from_error(excinfo.value)
    assert evidence is not None
    assert evidence.terminal_errors == ()
    assert any("stop failed" in error for error in evidence.cleanup_errors)


def test_run_protocol_probe_records_a_provider_stop_failure_onto_the_primary_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``stop()`` failure while a primary failure is already in flight must not replace it."""

    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec()
    deadline = Deadline.start(monotonic_clock, 30.0)
    original_stop = protocol_module.SubprocessAdapterRuntimeProvider.stop

    def failing_stop(self: SubprocessAdapterRuntimeProvider, adapter_runtime: AdapterRuntime) -> None:
        original_stop(self, adapter_runtime)  # still reap the real process
        raise TimeoutError("stop failed")

    monkeypatch.setattr(protocol_module.SubprocessAdapterRuntimeProvider, "stop", failing_stop)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> None:
        del client
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom") as excinfo:
        run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    notes = getattr(excinfo.value, "__notes__", ())
    assert any("stop failed" in note for note in notes)
    evidence = protocol_session_from_error(excinfo.value)
    assert evidence is not None
    assert evidence.terminal_errors == ()
    assert any("stop failed" in error for error in evidence.cleanup_errors)


def test_run_protocol_probe_makes_a_graceful_shutdown_failure_the_primary_when_there_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No primary failure yet -- the shutdown failure itself becomes the primary, and
    ``provider.stop()`` must still run regardless."""

    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec()
    deadline = Deadline.start(monotonic_clock, 30.0)
    stop_calls: list[AdapterRuntime] = []
    original_stop = protocol_module.SubprocessAdapterRuntimeProvider.stop

    def spy_stop(self: SubprocessAdapterRuntimeProvider, adapter_runtime: AdapterRuntime) -> None:
        stop_calls.append(adapter_runtime)
        original_stop(self, adapter_runtime)

    monkeypatch.setattr(protocol_module.SubprocessAdapterRuntimeProvider, "stop", spy_stop)

    def failing_shutdown(self: SyncLspClient, *, timeout: float = 2.0) -> None:
        raise TimeoutError("shutdown failed")

    monkeypatch.setattr(SyncLspClient, "shutdown", failing_shutdown)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> str:
        result = client.request("textDocument/hover", {})
        assert isinstance(result, dict)
        return str(result["echoed"])

    with pytest.raises(TimeoutError, match="shutdown failed") as excinfo:
        run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    assert len(stop_calls) == 1
    evidence = protocol_session_from_error(excinfo.value)
    assert evidence is not None
    assert evidence.terminal_errors == ()
    assert any("shutdown failed" in error for error in evidence.cleanup_errors)


def test_run_protocol_probe_records_a_shutdown_failure_onto_the_primary_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec()
    deadline = Deadline.start(monotonic_clock, 30.0)

    def failing_shutdown(self: SyncLspClient, *, timeout: float = 2.0) -> None:
        raise TimeoutError("shutdown failed")

    monkeypatch.setattr(SyncLspClient, "shutdown", failing_shutdown)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> None:
        del client
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom") as excinfo:
        run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    notes = getattr(excinfo.value, "__notes__", ())
    assert any("shutdown failed" in note for note in notes)
    evidence = protocol_session_from_error(excinfo.value)
    assert evidence is not None
    assert evidence.terminal_errors == ()
    assert any("shutdown failed" in error for error in evidence.cleanup_errors)


def test_run_protocol_probe_never_launches_when_deadline_already_expired(tmp_path: Path) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec()
    clock = _FakeClock()
    deadline = Deadline.start(clock, 1.0)
    clock.advance(2.0)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> None:
        del client
        raise AssertionError("session must never run when the deadline is already expired")

    with pytest.raises(DeadlineExceeded):
        run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)


def test_run_protocol_probe_raises_deadline_exceeded_when_session_overruns(tmp_path: Path) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec()
    clock = _FakeClock()
    deadline = Deadline.start(clock, 5.0)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> str:
        result = client.request("textDocument/hover", {})
        assert isinstance(result, dict)
        clock.advance(10.0)
        return str(result["echoed"])

    with pytest.raises(DeadlineExceeded):
        run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)


# --- Minor 4: honest minimum remaining budget for graceful shutdown ------------------


def test_run_protocol_probe_skips_the_graceful_shutdown_handshake_once_the_budget_is_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An expired budget must not be pretended-clean by a token near-zero shutdown attempt (M10)."""

    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec()
    clock = _FakeClock()
    deadline = Deadline.start(clock, 5.0)
    shutdown_calls: list[float] = []
    original_shutdown = SyncLspClient.shutdown

    def spy_shutdown(self: SyncLspClient, *, timeout: float = 2.0) -> None:
        shutdown_calls.append(timeout)
        original_shutdown(self, timeout=timeout)

    monkeypatch.setattr(SyncLspClient, "shutdown", spy_shutdown)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> str:
        result = client.request("textDocument/hover", {})
        assert isinstance(result, dict)
        clock.advance(10.0)  # exhausts the deadline before cleanup runs
        return str(result["echoed"])

    with pytest.raises(DeadlineExceeded):
        run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    # SubprocessAdapterRuntimeProvider.stop() always makes its own defensive shutdown call
    # (through the same SyncLspClient.shutdown this spy intercepts); exactly one call means
    # run_protocol_probe's own graceful-handshake attempt was skipped, as required.
    assert len(shutdown_calls) == 1


def test_run_protocol_probe_skips_graceful_shutdown_when_remaining_budget_is_below_the_honest_minimum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec()
    clock = _FakeClock()
    deadline = Deadline.start(clock, 5.0)
    minimum = protocol_module._GRACEFUL_SHUTDOWN_MINIMUM_SECONDS
    shutdown_calls: list[float] = []
    original_shutdown = SyncLspClient.shutdown

    def spy_shutdown(self: SyncLspClient, *, timeout: float = 2.0) -> None:
        shutdown_calls.append(timeout)
        original_shutdown(self, timeout=timeout)

    monkeypatch.setattr(SyncLspClient, "shutdown", spy_shutdown)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> str:
        result = client.request("textDocument/hover", {})
        assert isinstance(result, dict)
        clock.advance(5.0 - minimum / 2)  # leaves less than the honest minimum remaining, but still positive
        return str(result["echoed"])

    session_result = run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    assert session_result.result == "textDocument/hover"
    assert len(shutdown_calls) == 1  # only provider.stop()'s own defensive call


def test_run_protocol_probe_attempts_graceful_shutdown_when_remaining_budget_meets_the_honest_minimum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec()
    clock = _FakeClock()
    deadline = Deadline.start(clock, 5.0)
    minimum = protocol_module._GRACEFUL_SHUTDOWN_MINIMUM_SECONDS
    shutdown_calls: list[float] = []
    original_shutdown = SyncLspClient.shutdown

    def spy_shutdown(self: SyncLspClient, *, timeout: float = 2.0) -> None:
        shutdown_calls.append(timeout)
        original_shutdown(self, timeout=timeout)

    monkeypatch.setattr(SyncLspClient, "shutdown", spy_shutdown)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> str:
        result = client.request("textDocument/hover", {})
        assert isinstance(result, dict)
        clock.advance(5.0 - minimum * 2)  # comfortably above the honest minimum
        return str(result["echoed"])

    session_result = run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    assert session_result.result == "textDocument/hover"
    assert len(shutdown_calls) == 2  # run_protocol_probe's own attempt, then provider.stop()'s own


def test_run_protocol_probe_attempts_graceful_shutdown_while_budget_remains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec()
    deadline = Deadline.start(monotonic_clock, 30.0)
    shutdown_calls: list[float] = []
    original_shutdown = SyncLspClient.shutdown

    def spy_shutdown(self: SyncLspClient, *, timeout: float = 2.0) -> None:
        shutdown_calls.append(timeout)
        original_shutdown(self, timeout=timeout)

    monkeypatch.setattr(SyncLspClient, "shutdown", spy_shutdown)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> None:
        del client
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    # run_protocol_probe's own explicit attempt, then SubprocessAdapterRuntimeProvider.stop()'s
    # own defensive call: two calls means the graceful handshake really was attempted.
    assert shutdown_calls == [2.0, 2.0]


def test_run_protocol_probe_never_leaves_the_candidate_process_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec()
    deadline = Deadline.start(monotonic_clock, 30.0)
    processes: list[subprocess.Popen[bytes] | None] = []

    original_start = protocol_module.SubprocessAdapterRuntimeProvider.start

    def spy_start(
        self: SubprocessAdapterRuntimeProvider,
        *,
        notification_handler: Callable[[str, Any], None],
        terminal_handler: Callable[[BaseException], None],
    ) -> AdapterRuntime:
        adapter_runtime = original_start(
            self, notification_handler=notification_handler, terminal_handler=terminal_handler
        )
        processes.append(adapter_runtime.process)
        return adapter_runtime

    monkeypatch.setattr(protocol_module.SubprocessAdapterRuntimeProvider, "start", spy_start)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> None:
        client.request("textDocument/hover", {})

    run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    assert len(processes) == 1
    process = processes[0]
    assert process is not None
    assert process.wait(timeout=5) is not None


# --- run_protocol_probe: child environment isolation (C1, Important 3) ----------


_AMBIENT_LEAK_PROBE: dict[str, str] = {
    "CONDA_PREFIX": "/root/miniconda3/envs/ms",
    "PYTHONHOME": "/usr",
    "PYTHONPATH": "/some/ambient/module/path",
    "LD_PRELOAD": "/lib/evil.so",
    "NODE_OPTIONS": "--max-old-space-size=4096",
    "SSL_CERT_FILE": "/etc/ssl/ambient.pem",
    "PIP_INDEX_URL": "https://ambient.example/simple",
    "UV_INDEX_URL": "https://ambient.example/simple",
    "HTTP_PROXY": "http://proxy.internal:7890",
    "https_proxy": "http://proxy.internal:7890",
    "ALL_PROXY": "http://proxy.internal:7890",
    "no_proxy": "localhost",
}


def test_run_protocol_probe_child_environment_is_exactly_the_minimal_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Important 3: exact set equality with BACKEND_ENVIRONMENT_KEYS and exact values,
    including TMPDIR -- not a denylist of a few ambient names we happened to enumerate.

    ``LanguageServerSubprocessLauncher.launch`` execs the command through ``/bin/sh -c``
    (production, reused unmodified); ``PWD`` and ``LC_CTYPE`` are then self-injected by the
    shell (reflecting its own cwd) and by CPython's own locale coercion respectively --
    verified empirically with a *fully empty* environment (``env -i sh -c 'exec python3 -c
    ...'`` still observes exactly ``{PWD, LC_CTYPE}``), so they are not ambient leakage our
    env-dict argument could ever prevent. Excluded from the exact-allowlist comparison below
    and checked separately for a benign, non-attacker-controlled value.
    """

    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec(_fake_server_script(capabilities={}, echo_environment=True))
    deadline = Deadline.start(monotonic_clock, 30.0)
    for key, value in _AMBIENT_LEAK_PROBE.items():
        monkeypatch.setenv(key, value)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> dict[str, object]:
        result = client.request("textDocument/hover", {})
        assert isinstance(result, dict)
        environment = result["environment"]
        assert isinstance(environment, dict)
        return environment

    session_result = run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)
    observed = session_result.result

    shell_and_interpreter_injected = {"PWD", "LC_CTYPE"}
    assert set(observed) - shell_and_interpreter_injected == set(BACKEND_ENVIRONMENT_KEYS)
    assert {key: value for key, value in observed.items() if key not in shell_and_interpreter_injected} == {
        "HOME": str(runtime.home),
        "PATH": str(runtime.python.parent),
        "PYTHONPATH": "",
        "SERENA_LIGHT_SELECTED_PYTHON": str(runtime.python),
        "TMPDIR": str(runtime.root / "tmp"),
        "XDG_CACHE_HOME": str(runtime.cache),
        "XDG_CONFIG_HOME": str(runtime.config),
    }
    if "PWD" in observed:
        assert Path(str(observed["PWD"])).resolve() == tmp_path.resolve()


def test_run_protocol_probe_never_mutates_the_real_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec()
    deadline = Deadline.start(monotonic_clock, 30.0)
    for key, value in _AMBIENT_LEAK_PROBE.items():
        monkeypatch.setenv(key, value)
    before = dict(os.environ)

    def session(client: SyncLspClient, _providers: RawLspProviders) -> None:
        client.request("textDocument/hover", {})

    run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    assert dict(os.environ) == before


# --- Source ownership -----------------------------------------------------------


def _imported_module_names(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def _protocol_module_tree() -> ast.Module:
    source = (_REPO_ROOT / "scripts" / "backend_eval" / "protocol.py").read_text(encoding="utf-8")
    return ast.parse(source, filename="protocol.py")


def test_protocol_module_never_imports_workspace_runtime_or_language_adapter() -> None:
    tree = _protocol_module_tree()

    for module in _imported_module_names(tree):
        assert not module.startswith("serena_light.workspace"), module

    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)
    assert "LanguageAdapter" not in imported_names
    assert "WorkspaceRuntime" not in imported_names


def test_protocol_module_imports_the_shared_diagnostics_constant_without_a_local_duplicate() -> None:
    """Minor 7: protocol.py must reuse models.DIAGNOSTICS_MODES, never a second local set."""

    tree = _protocol_module_tree()

    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)
    assert "DIAGNOSTICS_MODES" in imported_names

    for node in ast.walk(tree):
        literal: set[object] | None = None
        if isinstance(node, ast.Set):
            values = [elt.value for elt in node.elts if isinstance(elt, ast.Constant)]
            if len(values) == len(node.elts):
                literal = set(values)
        assert literal != {"push", "pull"}, "protocol.py must not locally duplicate the diagnostics-mode set"


def test_src_serena_light_never_imports_backend_eval_evaluation_code() -> None:
    root = _REPO_ROOT / "src" / "serena_light"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imported_module_names(tree):
            if module == "scripts" or module.startswith("scripts."):
                offenders.append(f"{path}: {module}")
    assert offenders == []
