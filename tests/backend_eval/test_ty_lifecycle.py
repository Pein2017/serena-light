"""Real lifecycle and fault acceptance for the locked ty 0.0.70 server.

The protocol plane deliberately starts the candidate directly.  These tests therefore use
the Phase-1-published runtime and the shared production-shaped process/transport runner; they
do not construct ``LanguageAdapter`` or ``WorkspaceRuntime`` and do not prepare a new runtime.
The external-root marker proves every test leaves the live ``/data/ms-swift`` input unchanged.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import psutil
import pytest

import scripts.backend_eval.protocol as protocol_module
import scripts.backend_eval.ty_probe as ty_probe_module
from scripts.backend_eval.models import CandidateProtocolOutcome, ServiceConfigIdentity
from scripts.backend_eval.process import Deadline, monotonic_clock
from scripts.backend_eval.protocol import (
    ProtocolSession,
    protocol_session_from_error,
    run_protocol_probe,
)
from scripts.backend_eval.runtime import (
    BACKEND_ENVIRONMENT_KEYS,
    CandidateRuntime,
    minimal_backend_environment,
)
from scripts.backend_eval.ty_probe import run_ty_capability_probe, ty_protocol_spec
from serena_light.lsp.adapter import (
    AdapterRuntime,
    RawLspProviders,
    SubprocessAdapterRuntimeProvider,
)
from serena_light.lsp.client import (
    CONTENT_MODIFIED,
    LspResponseError,
    LspTransportClosed,
    SyncLspClient,
)

MS_SWIFT = Path("/data/ms-swift")
KNOWN_FILE = Path("swift/infer_engine/lmdeploy_engine.py")
KNOWN_POSITION = (14, 25)
REQUEST_CANCELLED = -32800

pytestmark = [
    pytest.mark.timeout(180),
    pytest.mark.external_repo(
        root=str(MS_SWIFT),
        snapshot_env="SERENA_LIGHT_MS_SWIFT_SNAPSHOT",
    ),
]


@dataclass(frozen=True, slots=True)
class _StartedProcess:
    process: subprocess.Popen[bytes]
    process_group: int


@pytest.fixture(scope="module")
def locked_ty_runtime() -> CandidateRuntime:
    runtime = ty_probe_module._prepared_candidate_runtime()
    assert runtime.ty.is_file()
    assert runtime.python.is_file()
    return runtime


@pytest.fixture(scope="module")
def real_capability_outcome(locked_ty_runtime: CandidateRuntime) -> CandidateProtocolOutcome:
    return _real_capability_outcome(locked_ty_runtime)


def _real_capability_outcome(runtime: CandidateRuntime) -> CandidateProtocolOutcome:
    return run_ty_capability_probe(
        runtime,
        MS_SWIFT,
        KNOWN_FILE,
        KNOWN_POSITION,
        deadline=Deadline.start(monotonic_clock, 90.0),
    )


def _ty_service_config(runtime: CandidateRuntime) -> ServiceConfigIdentity:
    matches = tuple(item for item in runtime.service_configs if item.backend == "ty")
    assert len(matches) == 1
    return matches[0]


def _run_real_session[T](
    runtime: CandidateRuntime,
    session: Callable[[SyncLspClient, RawLspProviders], T],
    *,
    deadline: Deadline | None = None,
    spec_transform: Callable[[Any], Any] | None = None,
) -> ProtocolSession[T]:
    spec = ty_protocol_spec(runtime, _ty_service_config(runtime))
    if spec_transform is not None:
        spec = spec_transform(spec)
    return run_protocol_probe(
        spec,
        runtime,
        MS_SWIFT,
        deadline=deadline or Deadline.start(monotonic_clock, 90.0),
        session=session,
    )


def _notify_open_document(client: SyncLspClient, runtime: CandidateRuntime) -> str:
    source = MS_SWIFT / KNOWN_FILE
    source_uri = source.as_uri()
    client.notify(
        "textDocument/didOpen",
        {
            "textDocument": {
                "uri": source_uri,
                "languageId": "python",
                "version": 1,
                "text": source.read_text(encoding="utf-8"),
            }
        },
    )
    return source_uri


def _capture_started_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> list[_StartedProcess]:
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

    monkeypatch.setattr(
        protocol_module.SubprocessAdapterRuntimeProvider,
        "start",
        spy_start,
    )
    return captured


def _process_group_has_live_member(process_group: int) -> bool:
    for candidate in psutil.process_iter(["pid", "status"]):
        try:
            if candidate.info["status"] == psutil.STATUS_ZOMBIE:
                continue
            if os.getpgid(candidate.pid) == process_group:
                return True
        except (OSError, psutil.Error):
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
    assert started.process.wait(timeout=5.0) is not None
    assert _wait_until(
        lambda: not _process_group_has_live_member(started.process_group),
        5.0,
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


def test_real_ty_full_lifecycle_is_cold_bounded_and_not_an_empty_success(
    real_capability_outcome: CandidateProtocolOutcome,
) -> None:
    outcome = real_capability_outcome
    missing_evidence = tuple(
        capability
        for capability in outcome.capabilities
        if capability.advertised and capability.normalized_valid is not True
    )

    assert outcome.engine_version == "0.0.70"
    assert 0 < outcome.lifecycle.cold_readiness_seconds < 90
    assert outcome.lifecycle.diagnostics_mode == "pull"
    assert missing_evidence
    assert outcome.gate_disposition == "fail"
    assert outcome.lifecycle.shutdown_clean is True
    assert outcome.lifecycle.cleanup_clean is True


def test_real_ty_full_lifecycle_pairs_current_implementation_advertisement_with_result(
    real_capability_outcome: CandidateProtocolOutcome,
) -> None:
    outcome = real_capability_outcome
    implementation = next(
        item for item in outcome.capabilities if item.name == "implementation"
    )

    # The locked 0.0.70 server currently advertises this provider.  Task 6 must carry that
    # current initialize fact through the full lifecycle rather than preserve Task 3's older
    # hypothetical negative branch as though it had actually been observed.
    assert outcome.raw_providers.implementation is True
    assert implementation.advertised is True
    assert implementation.accepted is True
    assert implementation.normalized_valid is False
    assert implementation.notes == "normalization returned no evidence"


def test_real_ty_uses_pull_diagnostics_and_returns_a_valid_report(
    locked_ty_runtime: CandidateRuntime,
) -> None:
    deadline = Deadline.start(monotonic_clock, 90.0)

    def session(
        client: SyncLspClient,
        _providers: RawLspProviders,
    ) -> object:
        source_uri = _notify_open_document(client, locked_ty_runtime)
        try:
            return client.request(
                "textDocument/diagnostic",
                {"textDocument": {"uri": source_uri}},
                timeout=deadline.remaining(),
            )
        finally:
            client.notify(
                "textDocument/didClose",
                {"textDocument": {"uri": source_uri}},
            )

    protocol_session = _run_real_session(
        locked_ty_runtime,
        session,
        deadline=deadline,
    )
    report = cast("Mapping[str, object]", protocol_session.result)

    assert protocol_session.diagnostic_provider is True
    assert report["kind"] in {"full", "unchanged"}
    if report["kind"] == "full":
        assert isinstance(report.get("items"), list)
    else:
        assert isinstance(report.get("resultId"), str)
    assert protocol_session.cleanup_errors == ()
    assert protocol_session.exit_status == 0


def test_real_ty_counts_content_modified_and_request_cancelled_without_retry(
    locked_ty_runtime: CandidateRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_request = SyncLspClient.request
    methods: Counter[str] = Counter()
    response_codes: list[int] = []
    observed_retry_sets: list[frozenset[str]] = []

    def spy_request(
        self: SyncLspClient,
        method: str,
        params: Any = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        methods[method] += 1
        observed_retry_sets.append(self._retry_methods)
        try:
            return original_request(self, method, params, timeout=timeout)
        except LspResponseError as error:
            response_codes.append(error.code)
            raise

    monkeypatch.setattr(SyncLspClient, "request", spy_request)
    outcome = _real_capability_outcome(locked_ty_runtime)

    assert observed_retry_sets
    assert all(retry_methods == frozenset() for retry_methods in observed_retry_sets)
    assert outcome.lifecycle.retry_seam_disabled is True
    assert outcome.lifecycle.content_modified_count == response_codes.count(
        CONTENT_MODIFIED
    )
    assert outcome.lifecycle.request_cancelled_count == response_codes.count(
        REQUEST_CANCELLED
    )
    for method in (
        "textDocument/definition",
        "textDocument/documentSymbol",
        "textDocument/implementation",
        "textDocument/references",
        "workspace/symbol",
    ):
        assert methods[method] == 1


@pytest.mark.parametrize(
    ("response_code", "counter_name"),
    [
        (CONTENT_MODIFIED, "content_modified_count"),
        (REQUEST_CANCELLED, "request_cancelled_count"),
    ],
)
def test_real_ty_observes_each_retryable_error_once_without_hidden_retry(
    locked_ty_runtime: CandidateRuntime,
    monkeypatch: pytest.MonkeyPatch,
    response_code: int,
    counter_name: str,
) -> None:
    """Inject the server-returned code at the real-client boundary after real initialize.

    Decision P2-2 forbids manufacturing ``$/cancelRequest`` or a raw request-ID channel.
    The locked ty subprocess still handles initialize, every other semantic request, and
    shutdown; only the selected real client's references response is replaced by the exact
    server error to deterministically prove one-attempt observation and counting.
    """

    original_request = SyncLspClient.request
    references_attempts = 0

    def inject_response_error(
        self: SyncLspClient,
        method: str,
        params: Any = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        nonlocal references_attempts
        if method == "textDocument/references":
            references_attempts += 1
            assert self._retry_methods == frozenset()
            raise LspResponseError(response_code, "injected lifecycle observation")
        return original_request(self, method, params, timeout=timeout)

    monkeypatch.setattr(SyncLspClient, "request", inject_response_error)
    outcome = _real_capability_outcome(locked_ty_runtime)
    references = next(
        item for item in outcome.capabilities if item.name == "references"
    )

    assert references_attempts == 1
    assert references.accepted is False
    assert references.normalized_valid is False
    assert str(response_code) in references.notes
    assert getattr(outcome.lifecycle, counter_name) == 1
    other_counter = (
        outcome.lifecycle.request_cancelled_count
        if counter_name == "content_modified_count"
        else outcome.lifecycle.content_modified_count
    )
    assert other_counter == 0
    assert outcome.lifecycle.retry_seam_disabled is True
    assert outcome.lifecycle.shutdown_clean is True
    assert outcome.lifecycle.cleanup_clean is True


def test_real_ty_bounded_request_timeout_is_typed_and_reaped(
    locked_ty_runtime: CandidateRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_started_processes(monkeypatch)
    deadline = Deadline.start(monotonic_clock, 90.0)

    def session(
        client: SyncLspClient,
        _providers: RawLspProviders,
    ) -> object:
        return client.request(
            "workspace/symbol",
            {"query": "LMDeploy"},
            timeout=1e-9,
        )

    with pytest.raises(TimeoutError, match="LSP request") as raised:
        _run_real_session(locked_ty_runtime, session, deadline=deadline)

    evidence = protocol_session_from_error(raised.value)
    assert evidence is not None
    assert evidence.result is None
    assert evidence.cleanup_errors == ()
    _assert_processes_reaped(captured)


def test_real_ty_crash_is_detected_and_its_process_group_is_reaped(
    locked_ty_runtime: CandidateRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_started_processes(monkeypatch)

    def session(
        client: SyncLspClient,
        _providers: RawLspProviders,
    ) -> None:
        assert len(captured) == 1
        process = captured[0].process
        process.kill()
        process.wait(timeout=5.0)
        assert _wait_until(lambda: not client.is_running, 5.0)
        client.request("workspace/symbol", {"query": "LMDeploy"}, timeout=5.0)

    with pytest.raises(LspTransportClosed) as raised:
        _run_real_session(locked_ty_runtime, session)

    evidence = protocol_session_from_error(raised.value)
    assert evidence is not None
    assert evidence.result is None
    assert evidence.exit_status == -signal.SIGKILL
    assert evidence.terminal_errors
    assert evidence.cleanup_errors == ()
    _assert_processes_reaped(captured)


def test_real_ty_graceful_shutdown_leaves_no_parent_or_process_group(
    locked_ty_runtime: CandidateRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _capture_started_processes(monkeypatch)

    def session(
        _client: SyncLspClient,
        _providers: RawLspProviders,
    ) -> str:
        return "initialized"

    protocol_session = _run_real_session(locked_ty_runtime, session)

    assert protocol_session.result == "initialized"
    assert protocol_session.terminal_errors == ()
    assert protocol_session.cleanup_errors == ()
    assert protocol_session.exit_status == 0
    _assert_processes_reaped(captured)


def test_real_ty_child_rejects_poisoned_proxy_and_ambient_environment(
    locked_ty_runtime: CandidateRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poison = {
        "HTTP_PROXY": "http://127.0.0.1:9/forbidden-http",
        "https_proxy": "http://127.0.0.1:9/forbidden-https",
        "ALL_PROXY": "socks5://127.0.0.1:9/forbidden-all",
        "no_proxy": "attacker.invalid",
        "PYTHONHOME": "/tmp/forbidden-python-home",
        "CONDA_PREFIX": "/tmp/forbidden-conda",
        "PIP_INDEX_URL": "https://forbidden.invalid/simple",
        "UV_INDEX_URL": "https://forbidden.invalid/simple",
        "SERENA_LIGHT_TEST_SECRET": "must-not-reach-locked-ty",
    }
    for key, value in poison.items():
        monkeypatch.setenv(key, value)
    captured = _capture_started_processes(monkeypatch)

    def session(
        _client: SyncLspClient,
        _providers: RawLspProviders,
    ) -> dict[str, str]:
        assert len(captured) == 1
        return _read_process_environment(captured[0].process)

    protocol_session = _run_real_session(locked_ty_runtime, session)
    observed = protocol_session.result
    frozen_ms_interpreter = Path(
        next(
            identity.interpreter_path
            for identity in locked_ty_runtime.environments
            if identity.name == "ms"
        )
    )
    selected_interpreter = protocol_session.engine.interpreter
    assert selected_interpreter is not None
    assert selected_interpreter == frozen_ms_interpreter
    expected = minimal_backend_environment(
        locked_ty_runtime,
        selected_interpreter,
    )

    assert locked_ty_runtime.root.is_relative_to(
        Path("/data/CoordExp/.codex/runtime/serena-light/backend-eval")
    )
    assert all(key not in observed for key in poison)
    assert not any(key.upper().endswith("_PROXY") for key in observed)
    assert {key: observed[key] for key in BACKEND_ENVIRONMENT_KEYS} == expected
    assert set(observed) - {"PWD", "LC_CTYPE"} == set(BACKEND_ENVIRONMENT_KEYS)
    _assert_processes_reaped(captured)


def test_real_ty_stderr_secret_evidence_is_redacted_and_bounded(
    locked_ty_runtime: CandidateRuntime,
) -> None:
    bearer = "ty-lifecycle-fake-bearer-4af2"
    password = "ty-lifecycle-fake-password-9c71"

    def add_stderr_fault(spec: Any) -> Any:
        def build_command(runtime: CandidateRuntime) -> tuple[str, ...]:
            script = (
                "printf '%s\\n' "
                f"'Authorization: Bearer {bearer}' "
                f"'password={password}' >&2; "
                'exec "$1" server'
            )
            return (
                "/bin/sh",
                "-c",
                script,
                "ty-lifecycle-redaction-wrapper",
                str(runtime.ty),
            )

        return replace(spec, build_command=build_command)

    def session(
        _client: SyncLspClient,
        _providers: RawLspProviders,
    ) -> None:
        return None

    protocol_session = _run_real_session(
        locked_ty_runtime,
        session,
        spec_transform=add_stderr_fault,
    )

    assert bearer not in protocol_session.stderr_tail
    assert password not in protocol_session.stderr_tail
    assert protocol_session.stderr_tail.count("<redacted>") >= 2
    assert len(protocol_session.stderr_tail) <= 1024
    assert protocol_session.cleanup_errors == ()
    assert protocol_session.exit_status == 0
