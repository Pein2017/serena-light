"""Real Pyright lifecycle, fault, environment, and redaction coverage.

The protocol plane deliberately has no ``WorkspaceRuntime`` readiness or lease state.  These
tests therefore exercise the strongest evidence this plane owns: a fresh locked Pyright
process, one immediately opened real document, one non-empty semantic result, observed push
diagnostics, the production client's disabled retry seam, and the shared runner's cleanup.

``ContentModified`` and ``RequestCancelled`` are not induced with timing races.  The former
is injected at the real client's single-request boundary while a real Pyright session is
alive; the latter uses the same production ``SyncLspClient.request`` path with a deterministic
response double.  This proves observation/counting and one-attempt behavior without claiming
that a particular run happened to make Pyright emit either code.
"""

from __future__ import annotations

import io
import os
import signal
import subprocess
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import psutil
import pytest

from scripts.backend_eval import protocol as protocol_module
from scripts.backend_eval import pyright_probe as pyright_probe_module
from scripts.backend_eval.manifests import read_stable_source_text
from scripts.backend_eval.models import LifecycleEvidence
from scripts.backend_eval.process import Deadline, monotonic_clock
from scripts.backend_eval.protocol import (
    BackendProtocolSpec,
    ProtocolSession,
    protocol_session_from_error,
    run_protocol_probe,
)
from scripts.backend_eval.pyright_probe import (
    _prepared_candidate_runtime,
    pyright_protocol_spec,
    run_pyright_capability_probe,
)
from scripts.backend_eval.runtime import CandidateRuntime
from serena_light.lsp.adapter import (
    AdapterRuntime,
    EngineMetadata,
    RawLspProviders,
    SubprocessAdapterRuntimeProvider,
)
from serena_light.lsp.client import (
    CONTENT_MODIFIED,
    LspResponseError,
    LspTransportClosed,
    SyncLspClient,
)
from serena_light.lsp.positions import PositionEncoding
from serena_light.lsp.pyright import PyrightFacts
from serena_light.workspace.identity import MS_INTERPRETER

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MS_SWIFT = Path("/data/ms-swift")
_TARGET = _MS_SWIFT / "swift/infer_engine/lmdeploy_engine.py"
_TARGET_POSITION = (14, 25)
_MS_SITE_PACKAGES = MS_INTERPRETER.parents[1] / "lib" / "python3.12" / "site-packages"
_TRANSFORMERS_ROOT = (_MS_SITE_PACKAGES / "transformers").resolve(strict=True)
_REQUEST_CANCELLED = -32800
_REAL_TEST_DEADLINE_SECONDS = 90.0
_PROCESS_REAP_SECONDS = 5.0
_PUSH_DIAGNOSTICS_WAIT_SECONDS = 15.0

pytestmark = [
    pytest.mark.timeout(120),
    pytest.mark.external_repo(
        root=str(_MS_SWIFT),
        snapshot_env="SERENA_LIGHT_MS_SWIFT_SNAPSHOT",
    ),
    pytest.mark.external_repo(
        root=str(_TRANSFORMERS_ROOT),
        snapshot_env="SERENA_LIGHT_TRANSFORMERS_SNAPSHOT",
    ),
]


@dataclass(frozen=True, slots=True)
class _ProcessIdentity:
    process: subprocess.Popen[bytes]
    pid: int
    process_group: int
    create_time: float


@dataclass(frozen=True, slots=True)
class _HealthySessionResult:
    readiness_seconds: float
    symbol_count: int
    diagnostics_published: bool
    content_modified_code: int
    content_modified_attempts: int
    retry_methods: frozenset[str]
    environment: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _HealthyObservation:
    session: ProtocolSession[_HealthySessionResult]
    lifecycle: LifecycleEvidence
    process: _ProcessIdentity


_AMBIENT_POISON = {
    "ALL_PROXY": "http://fake-user:fake-password@127.0.0.1:9090",
    "HTTP_PROXY": "http://fake-user:fake-password@127.0.0.1:9090",
    "HTTPS_PROXY": "http://fake-user:fake-password@127.0.0.1:9090",
    "all_proxy": "http://fake-user:fake-password@127.0.0.1:9090",
    "http_proxy": "http://fake-user:fake-password@127.0.0.1:9090",
    "https_proxy": "http://fake-user:fake-password@127.0.0.1:9090",
    "NO_PROXY": "localhost,127.0.0.1",
    "no_proxy": "localhost,127.0.0.1",
    "CONDA_PREFIX": "/ambient/conda",
    "LD_PRELOAD": "/ambient/not-loaded.so",
    "NODE_OPTIONS": "--require=/ambient/not-loaded.js",
    "PIP_INDEX_URL": "https://fake-user:fake-password@example.invalid/simple",
    "PYTHONHOME": "/ambient/python",
    "PYTHONPATH": "/ambient/modules",
    "SERENA_LIGHT_TEST_SECRET": "fake-lifecycle-secret-value",
    "UV_INDEX_URL": "https://fake-user:fake-password@example.invalid/simple",
}


def _locked_facts() -> PyrightFacts:
    return PyrightFacts.locked(root=_REPO_ROOT, interpreter=MS_INTERPRETER)


def _process_identity(process: subprocess.Popen[bytes]) -> _ProcessIdentity:
    return _ProcessIdentity(
        process=process,
        pid=process.pid,
        process_group=os.getpgid(process.pid),
        create_time=psutil.Process(process.pid).create_time(),
    )


def _install_process_spy(
    monkeypatch: pytest.MonkeyPatch,
    *,
    notification_handler: Callable[[str, Any], None] | None = None,
) -> list[tuple[AdapterRuntime, _ProcessIdentity]]:
    captured: list[tuple[AdapterRuntime, _ProcessIdentity]] = []
    original_start = protocol_module.SubprocessAdapterRuntimeProvider.start
    replacement = notification_handler

    def bound_spy_start(
        self: SubprocessAdapterRuntimeProvider,
        *,
        notification_handler: Callable[[str, Any], None],
        terminal_handler: Callable[[BaseException], None],
    ) -> AdapterRuntime:
        runtime = original_start(
            self,
            notification_handler=(replacement or notification_handler),
            terminal_handler=terminal_handler,
        )
        process = runtime.process
        assert process is not None
        captured.append((runtime, _process_identity(process)))
        return runtime

    monkeypatch.setattr(
        protocol_module.SubprocessAdapterRuntimeProvider,
        "start",
        bound_spy_start,
    )
    return captured


def _read_process_environment(pid: int) -> dict[str, str]:
    raw = (Path("/proc") / str(pid) / "environ").read_bytes()
    environment: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        key, separator, value = entry.partition(b"=")
        assert separator == b"="
        environment[key.decode("utf-8", "strict")] = value.decode("utf-8", "strict")
    return environment


def _live_process_group_members(process_group: int) -> tuple[int, ...]:
    members: list[int] = []
    for process in psutil.process_iter(["pid"]):
        try:
            if os.getpgid(process.pid) == process_group:
                members.append(process.pid)
        except (OSError, psutil.Error):
            continue
    return tuple(sorted(members))


def _assert_process_tree_reaped(identity: _ProcessIdentity) -> None:
    identity.process.wait(timeout=_PROCESS_REAP_SECONDS)
    deadline = time.monotonic() + _PROCESS_REAP_SECONDS
    while time.monotonic() < deadline:
        try:
            current = psutil.Process(identity.pid)
        except psutil.NoSuchProcess:
            current = None
        if current is not None and current.create_time() != identity.create_time:
            current = None
        members = _live_process_group_members(identity.process_group)
        if current is None and not members:
            return
        time.sleep(0.02)
    raise AssertionError(
        "Pyright process tree survived cleanup: "
        f"pid={identity.pid} pgid={identity.process_group} "
        f"members={_live_process_group_members(identity.process_group)}"
    )


def _expected_minimal_environment(
    runtime: CandidateRuntime,
    selected_interpreter: Path,
) -> dict[str, str]:
    return {
        "HOME": str(runtime.home),
        "PATH": str(runtime.python.parent),
        "PYTHONPATH": "",
        "SERENA_LIGHT_SELECTED_PYTHON": str(selected_interpreter),
        "TMPDIR": str(runtime.root / "tmp"),
        "XDG_CACHE_HOME": str(runtime.cache),
        "XDG_CONFIG_HOME": str(runtime.config),
    }


def _minimal_environment_matches(
    observed: Mapping[str, str],
    runtime: CandidateRuntime,
    selected_interpreter: Path,
) -> bool:
    # ``/bin/sh`` and CPython may inject these after the explicit environment has already
    # been applied.  Neither comes from ambient attacker-controlled state; this is the same
    # boundary pinned by the shared runner's real-process test.
    child_injected = {"PWD", "LC_CTYPE"}
    controlled = {key: value for key, value in observed.items() if key not in child_injected}
    return controlled == _expected_minimal_environment(runtime, selected_interpreter)


def _proxy_environment_absent(observed: Mapping[str, str]) -> bool:
    return not any(key.upper().endswith("_PROXY") for key in observed)


@pytest.fixture(scope="module")
def healthy_pyright_observation() -> _HealthyObservation:
    runtime = _prepared_candidate_runtime()
    facts = _locked_facts()
    spec = pyright_protocol_spec(runtime, facts, production_root=_REPO_ROOT)
    deadline = Deadline.start(monotonic_clock, _REAL_TEST_DEADLINE_SECONDS)
    source_text = read_stable_source_text(_MS_SWIFT, _TARGET, deadline=deadline)
    source_uri = _TARGET.as_uri()
    diagnostics_event = threading.Event()
    diagnostics_counts: list[int] = []

    def observe_notification(method: str, params: Any) -> None:
        if method != "textDocument/publishDiagnostics" or not isinstance(params, Mapping):
            return
        diagnostics = params.get("diagnostics")
        count = len(diagnostics) if isinstance(diagnostics, Sequence) else -1
        diagnostics_counts.append(count)
        diagnostics_event.set()

    with pytest.MonkeyPatch.context() as monkeypatch:
        for key, value in _AMBIENT_POISON.items():
            monkeypatch.setenv(key, value)
        captured = _install_process_spy(
            monkeypatch,
            notification_handler=observe_notification,
        )
        started = time.monotonic()

        def session(
            client: SyncLspClient,
            _providers: RawLspProviders,
        ) -> _HealthySessionResult:
            assert len(captured) == 1
            process = captured[0][1]
            environment = _read_process_environment(process.pid)
            retry_methods = client._retry_methods
            client.notify("workspace/didChangeConfiguration", {"settings": {}})
            client.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": source_uri,
                        "languageId": facts.language_id,
                        "version": 1,
                        "text": source_text,
                    }
                },
            )
            try:
                symbols = client.request(
                    "textDocument/documentSymbol",
                    {"textDocument": {"uri": source_uri}},
                    timeout=deadline.remaining(),
                )
                if not isinstance(symbols, Sequence) or isinstance(symbols, str | bytes):
                    raise AssertionError("Pyright documentSymbol must return a sequence")
                readiness_seconds = time.monotonic() - started
                diagnostics_published = diagnostics_event.wait(
                    timeout=min(_PUSH_DIAGNOSTICS_WAIT_SECONDS, deadline.remaining())
                )

                attempts = 0
                def content_modified_once(
                    method: str,
                    params: Any,
                    *,
                    timeout: float | None,
                ) -> object:
                    nonlocal attempts
                    del params, timeout
                    attempts += 1
                    raise LspResponseError(CONTENT_MODIFIED, f"injected for {method}")

                vars(client)["_request_once"] = content_modified_once
                try:
                    with pytest.raises(LspResponseError) as raised:
                        client.request(
                            "textDocument/definition",
                            {
                                "textDocument": {"uri": source_uri},
                                "position": {
                                    "line": _TARGET_POSITION[0],
                                    "character": _TARGET_POSITION[1],
                                },
                            },
                            timeout=deadline.remaining(),
                        )
                finally:
                    vars(client).pop("_request_once", None)
                return _HealthySessionResult(
                    readiness_seconds=readiness_seconds,
                    symbol_count=len(symbols),
                    diagnostics_published=diagnostics_published,
                    content_modified_code=raised.value.code,
                    content_modified_attempts=attempts,
                    retry_methods=retry_methods,
                    environment=environment,
                )
            finally:
                client.notify(
                    "textDocument/didClose",
                    {"textDocument": {"uri": source_uri}},
                )

        protocol_session = run_protocol_probe(
            spec,
            runtime,
            _MS_SWIFT,
            deadline=deadline,
            session=session,
        )

    assert len(captured) == 1
    process = captured[0][1]
    _assert_process_tree_reaped(process)
    result = protocol_session.result
    selected_interpreter = protocol_session.engine.interpreter
    assert selected_interpreter is not None
    lifecycle = LifecycleEvidence(
        cold_readiness_seconds=result.readiness_seconds,
        diagnostics_mode=spec.diagnostics_mode,
        content_modified_count=int(result.content_modified_code == CONTENT_MODIFIED),
        request_cancelled_count=0,
        retry_seam_disabled=not result.retry_methods,
        bounded_timeout_observed=False,
        crash_handled=False,
        shutdown_clean=(
            protocol_session.exit_status == 0
            and not protocol_session.terminal_errors
            and not protocol_session.cleanup_errors
        ),
        cleanup_clean=(
            not protocol_session.cleanup_errors
            and not _live_process_group_members(process.process_group)
        ),
        proxy_rejected=_proxy_environment_absent(result.environment),
        minimal_environment_verified=_minimal_environment_matches(
            result.environment,
            runtime,
            selected_interpreter,
        ),
        redaction_verified=False,
    )
    assert diagnostics_counts and all(count >= 0 for count in diagnostics_counts)
    return _HealthyObservation(
        session=protocol_session,
        lifecycle=lifecycle,
        process=process,
    )


def test_cold_readiness_never_reports_empty_success_as_ready(
    healthy_pyright_observation: _HealthyObservation,
) -> None:
    result = healthy_pyright_observation.session.result

    assert result.symbol_count > 0
    assert 0.0 < healthy_pyright_observation.lifecycle.cold_readiness_seconds < 120.0


def test_diagnostics_mode_is_recorded_push_for_pyright(
    healthy_pyright_observation: _HealthyObservation,
) -> None:
    assert healthy_pyright_observation.lifecycle.diagnostics_mode == "push"
    assert healthy_pyright_observation.session.diagnostic_provider is False
    assert healthy_pyright_observation.session.result.diagnostics_published is True


def test_content_modified_returns_the_documented_code_with_no_retry(
    healthy_pyright_observation: _HealthyObservation,
) -> None:
    result = healthy_pyright_observation.session.result

    assert result.content_modified_code == CONTENT_MODIFIED == -32801
    assert result.content_modified_attempts == 1
    assert healthy_pyright_observation.lifecycle.content_modified_count == 1
    assert healthy_pyright_observation.lifecycle.retry_seam_disabled is True


def test_graceful_shutdown_leaves_no_process(
    healthy_pyright_observation: _HealthyObservation,
) -> None:
    assert healthy_pyright_observation.process.process_group == healthy_pyright_observation.process.pid
    assert healthy_pyright_observation.session.exit_status == 0
    assert healthy_pyright_observation.lifecycle.shutdown_clean is True
    assert healthy_pyright_observation.lifecycle.cleanup_clean is True
    assert _live_process_group_members(healthy_pyright_observation.process.process_group) == ()


def test_proxy_variables_are_never_present_in_the_child_environment(
    healthy_pyright_observation: _HealthyObservation,
) -> None:
    environment = healthy_pyright_observation.session.result.environment

    assert healthy_pyright_observation.lifecycle.proxy_rejected is True
    assert not any(key.upper().endswith("_PROXY") for key in environment)


def test_minimal_environment_matches_minimal_backend_environment(
    healthy_pyright_observation: _HealthyObservation,
) -> None:
    runtime = _prepared_candidate_runtime()
    environment = healthy_pyright_observation.session.result.environment
    selected_interpreter = healthy_pyright_observation.session.engine.interpreter
    assert selected_interpreter is not None

    assert healthy_pyright_observation.lifecycle.minimal_environment_verified is True
    assert {key: value for key, value in environment.items() if key not in {"PWD", "LC_CTYPE"}} == (
        _expected_minimal_environment(runtime, selected_interpreter)
    )
    assert "SERENA_LIGHT_TEST_SECRET" not in environment


def test_request_cancelled_response_is_counted_once_without_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "known.py"
    target.write_text("class Known:\n    pass\n", encoding="utf-8")
    target_uri = target.as_uri()
    location = {
        "uri": target_uri,
        "range": {
            "start": {"line": 0, "character": 0},
            "end": {"line": 0, "character": 5},
        },
    }
    responses: dict[str, object] = {
        "textDocument/definition": LspResponseError(_REQUEST_CANCELLED, "cancelled once"),
        "textDocument/references": [location],
        "textDocument/implementation": [location],
        "textDocument/documentSymbol": [
            {
                "name": "Known",
                "kind": 5,
                "range": location["range"],
                "selectionRange": location["range"],
            }
        ],
        "workspace/symbol": [
            {"name": "Known", "kind": 5, "location": location}
        ],
    }
    requests: Counter[str] = Counter()
    client = SyncLspClient(io.BytesIO(), io.BytesIO())

    def request_once(
        method: str,
        params: Any,
        *,
        timeout: float | None,
    ) -> object:
        del params, timeout
        requests[method] += 1
        response = responses[method]
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr(client, "_request_once", request_once)
    runtime = cast(CandidateRuntime, object())
    monkeypatch.setattr(pyright_probe_module, "_prepared_candidate_runtime", lambda: runtime)

    def fake_runner(
        spec: BackendProtocolSpec,
        candidate_runtime: CandidateRuntime,
        workspace_root: Path,
        *,
        deadline: Deadline,
        session: Callable[[SyncLspClient, RawLspProviders], object],
    ) -> ProtocolSession[object]:
        del spec, candidate_runtime, workspace_root, deadline
        providers = RawLspProviders(
            definition=True,
            implementation=True,
            references=True,
            document_symbols=True,
            workspace_symbols=True,
        )
        return ProtocolSession(
            raw_providers=providers,
            diagnostic_provider=False,
            position_encoding=PositionEncoding.UTF16,
            engine=EngineMetadata(
                name="pyright",
                version="1.1.403",
                executable=Path("/runtime/pyright-langserver"),
                interpreter=Path("/runtime/python"),
            ),
            stderr_tail="",
            terminal_errors=(),
            cleanup_errors=(),
            exit_status=0,
            result=session(client, providers),
        )

    monkeypatch.setattr(pyright_probe_module, "run_protocol_probe", fake_runner)
    try:
        outcome = run_pyright_capability_probe(
            runtime,
            _locked_facts(),
            tmp_path,
            target,
            (0, 0),
            production_root=_REPO_ROOT,
            deadline=Deadline.start(monotonic_clock, 10.0),
        )
    finally:
        client.close()

    assert outcome.lifecycle.request_cancelled_count == 1
    assert outcome.lifecycle.content_modified_count == 0
    assert outcome.lifecycle.retry_seam_disabled is True
    assert requests["textDocument/definition"] == 1
    assert outcome.gate_disposition == "fail"


def test_bounded_request_timeout_raises_typed_timeout_and_cleans_up() -> None:
    runtime = _prepared_candidate_runtime()
    facts = _locked_facts()
    spec = pyright_protocol_spec(runtime, facts, production_root=_REPO_ROOT)
    deadline = Deadline.start(monotonic_clock, _REAL_TEST_DEADLINE_SECONDS)

    with pytest.MonkeyPatch.context() as monkeypatch:
        captured = _install_process_spy(monkeypatch)

        def session(client: SyncLspClient, _providers: RawLspProviders) -> None:
            client.request("workspace/symbol", {"query": "LmdeployEngine"}, timeout=1e-9)

        with pytest.raises(TimeoutError, match="LSP request") as raised:
            run_protocol_probe(
                spec,
                runtime,
                _MS_SWIFT,
                deadline=deadline,
                session=session,
            )

    assert len(captured) == 1
    evidence = protocol_session_from_error(raised.value)
    assert evidence is not None
    assert evidence.result is None
    assert evidence.cleanup_errors == ()
    _assert_process_tree_reaped(captured[0][1])


def test_crash_is_detected_and_process_tree_is_fully_reaped() -> None:
    runtime = _prepared_candidate_runtime()
    facts = _locked_facts()
    spec = pyright_protocol_spec(runtime, facts, production_root=_REPO_ROOT)
    deadline = Deadline.start(monotonic_clock, _REAL_TEST_DEADLINE_SECONDS)

    with pytest.MonkeyPatch.context() as monkeypatch:
        captured = _install_process_spy(monkeypatch)

        def session(client: SyncLspClient, _providers: RawLspProviders) -> None:
            assert len(captured) == 1
            os.kill(captured[0][1].pid, signal.SIGKILL)
            client.request("workspace/symbol", {"query": "LmdeployEngine"}, timeout=5.0)

        with pytest.raises(LspTransportClosed) as raised:
            run_protocol_probe(
                spec,
                runtime,
                _MS_SWIFT,
                deadline=deadline,
                session=session,
            )

    assert len(captured) == 1
    evidence = protocol_session_from_error(raised.value)
    assert evidence is not None
    assert evidence.result is None
    assert evidence.exit_status == -signal.SIGKILL
    assert evidence.terminal_errors
    assert evidence.cleanup_errors == ()
    _assert_process_tree_reaped(captured[0][1])


def test_stderr_and_environment_are_redacted_in_the_recorded_evidence() -> None:
    runtime = _prepared_candidate_runtime()
    facts = _locked_facts()
    direct_spec = pyright_protocol_spec(runtime, facts, production_root=_REPO_ROOT)
    bearer_secret = "fake-pyright-bearer-secret-abc123"
    password_secret = "fake-pyright-password-secret-xyz789"
    wrapper = (
        "import os,sys;"
        f"sys.stderr.write('Authorization: Bearer {bearer_secret}\\n'"
        f"+'password={password_secret}\\n');"
        "sys.stderr.flush();"
        "os.execv(sys.argv[1],sys.argv[1:])"
    )
    wrapped_command = (
        str(runtime.python),
        "-I",
        "-c",
        wrapper,
        *facts.command,
    )
    spec = replace(
        direct_spec,
        build_command=lambda _runtime: wrapped_command,
    )
    deadline = Deadline.start(monotonic_clock, _REAL_TEST_DEADLINE_SECONDS)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("SERENA_LIGHT_TEST_SECRET", bearer_secret)
        captured = _install_process_spy(monkeypatch)

        def session(client: SyncLspClient, _providers: RawLspProviders) -> bool:
            assert len(captured) == 1
            environment = _read_process_environment(captured[0][1].pid)
            client.request(
                "workspace/symbol",
                {"query": "__serena_light_redaction_probe__"},
                timeout=deadline.remaining(),
            )
            return (
                "SERENA_LIGHT_TEST_SECRET" not in environment
                and bearer_secret not in environment.values()
            )

        protocol_session = run_protocol_probe(
            spec,
            runtime,
            _MS_SWIFT,
            deadline=deadline,
            session=session,
        )

    assert len(captured) == 1
    _assert_process_tree_reaped(captured[0][1])
    recorded = repr(protocol_session)
    assert protocol_session.result is True
    assert bearer_secret not in protocol_session.stderr_tail
    assert password_secret not in protocol_session.stderr_tail
    assert bearer_secret not in recorded
    assert password_secret not in recorded
    assert protocol_session.stderr_tail.count("<redacted>") >= 3
    assert len(protocol_session.stderr_tail) <= 1024
