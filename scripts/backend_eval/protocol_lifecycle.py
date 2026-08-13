"""Consumable, real lifecycle evidence for the Phase 2 protocol gate.

The per-candidate lifecycle test modules prove individual scenarios, but pytest results are
not receipt evidence.  This module runs the same bounded scenarios through the shared
``run_protocol_probe`` seam and returns one typed, bounded record that Task 8 can consume.
It never shells out to pytest, never infers a passing field from test presence, and never
adds a retry or client-side ``$/cancelRequest`` path.
"""

from __future__ import annotations

import json
import math
import os
import signal
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import Any, Protocol, cast

import psutil

from scripts.backend_eval.manifests import read_stable_source_text
from scripts.backend_eval.models import DIAGNOSTICS_MODES, LifecycleEvidence
from scripts.backend_eval.process import Deadline, DeadlineExceeded
from scripts.backend_eval.protocol import (
    BackendProtocolSpec,
    ProtocolSession,
    protocol_session_from_error,
    redacted_evidence_text,
    run_protocol_probe,
)
from scripts.backend_eval.runtime import CandidateRuntime, minimal_backend_environment
from serena_light.lsp.adapter import RawLspProviders
from serena_light.lsp.client import (
    CONTENT_MODIFIED,
    LspResponseError,
    LspTransportClosed,
    SyncLspClient,
)

__all__ = [
    "LIFECYCLE_SCENARIOS",
    "LifecycleBatteryRequest",
    "LifecycleBatteryResult",
    "LifecycleInfrastructureError",
    "LifecycleScenarioEvidence",
    "LifecycleScenarioExecutor",
    "run_lifecycle_battery",
]

LIFECYCLE_SCENARIOS = (
    "cold_diagnostics",
    "content_modified",
    "request_cancelled",
    "bounded_timeout",
    "crash",
    "graceful_shutdown",
    "environment_redaction",
)

_CANDIDATES = frozenset({"pyright", "ty", "pyrefly"})
_REQUEST_CANCELLED = -32800
_MAX_DETAIL_CHARS = 512
_PROCESS_REAP_SECONDS = 5.0
_DIAGNOSTICS_WAIT_SECONDS = 15.0
_SYNTHETIC_BEARER = "serena-light-lifecycle-bearer-7fd33e"
_SYNTHETIC_PASSWORD = "serena-light-lifecycle-password-24bda1"
_ENVIRONMENT_MARKER = "SERENA_LIGHT_LIFECYCLE_ENV"
_ENVIRONMENT_LOCK = threading.Lock()
_AMBIENT_POISON = {
    "ALL_PROXY": "socks5://127.0.0.1:9/lifecycle-forbidden",
    "HTTP_PROXY": "http://127.0.0.1:9/lifecycle-forbidden",
    "HTTPS_PROXY": "http://127.0.0.1:9/lifecycle-forbidden",
    "all_proxy": "socks5://127.0.0.1:9/lifecycle-forbidden",
    "http_proxy": "http://127.0.0.1:9/lifecycle-forbidden",
    "https_proxy": "http://127.0.0.1:9/lifecycle-forbidden",
    "NO_PROXY": "attacker.invalid",
    "no_proxy": "attacker.invalid",
    "CONDA_PREFIX": "/ambient/forbidden-conda",
    "LD_PRELOAD": "/ambient/forbidden-preload.so",
    "NODE_OPTIONS": "--require=/ambient/forbidden.js",
    "PIP_INDEX_URL": "https://forbidden.invalid/simple",
    "PYTHONHOME": "/ambient/forbidden-python",
    "PYTHONPATH": "/ambient/forbidden-modules",
    "SERENA_LIGHT_LIFECYCLE_POISON": "must-not-reach-candidate",
    "UV_INDEX_URL": "https://forbidden.invalid/simple",
}


class LifecycleInfrastructureError(RuntimeError):
    """The harness could not prove a candidate-independent lifecycle precondition."""


def _environment_measurement(
    stderr: str,
) -> tuple[bool, bool, bool, dict[str, tuple[str, ...]]] | None:
    prefix = f"{_ENVIRONMENT_MARKER} "
    for line in reversed(stderr.splitlines()):
        if not line.startswith(prefix):
            continue
        parts = line.split(" ", 4)
        if len(parts) != 5 or parts[0] != _ENVIRONMENT_MARKER:
            return None
        flags: list[bool] = []
        for expected_name, token in zip(
            ("minimal", "proxy", "poison"), parts[1:4], strict=True
        ):
            name, separator, value = token.partition("=")
            if name != expected_name or separator != "=" or value not in {"0", "1"}:
                return None
            flags.append(value == "1")
        try:
            raw_keys = json.loads(parts[4])
        except (TypeError, ValueError):
            return None
        if not isinstance(raw_keys, Mapping) or set(raw_keys) != {
            "changed_keys",
            "extra_keys",
            "missing_keys",
        }:
            return None
        key_sets: dict[str, tuple[str, ...]] = {}
        for name in ("changed_keys", "extra_keys", "missing_keys"):
            values = raw_keys.get(name)
            if (
                not isinstance(values, Sequence)
                or isinstance(values, str | bytes)
                or any(not isinstance(value, str) for value in values)
            ):
                return None
            key_sets[name] = tuple(sorted(cast("Sequence[str]", values)))
        return flags[0], flags[1], flags[2], key_sets
    return None


def _environment_marker_detail(stderr: str) -> str:
    measurement = _environment_measurement(stderr)
    if measurement is None:
        return "environment measurement marker unavailable or malformed"
    minimal, proxy, poison, key_sets = measurement
    rendered = (
        f"{_ENVIRONMENT_MARKER} minimal={int(minimal)} proxy={int(proxy)} "
        f"poison={int(poison)} "
        + json.dumps(key_sets, sort_keys=True, separators=(",", ":"))
    )
    return redacted_evidence_text(rendered)[:_MAX_DETAIL_CHARS]


def _require_minimal_environment_measurement(stderr: str) -> tuple[bool, bool, bool]:
    measurement = _environment_measurement(stderr)
    detail = _environment_marker_detail(stderr)
    if measurement is None:
        raise LifecycleInfrastructureError(detail)
    minimal, proxy, poison, _key_sets = measurement
    if not minimal:
        raise LifecycleInfrastructureError(
            redacted_evidence_text(
                f"minimal backend environment measurement mismatch: {detail}"
            )[:_MAX_DETAIL_CHARS]
        )
    return minimal, proxy, poison


def _environment_wrapper_program() -> str:
    """Measure the exact inherited env and publish the marker after candidate stderr."""

    return (
        "import json,os,subprocess,sys;"
        "expected=json.loads(sys.argv[1]);"
        "actual={k:v for k,v in os.environ.items() if k not in {'PWD','LC_CTYPE'}};"
        "missing=sorted(set(expected)-set(actual));"
        "extra=sorted(set(actual)-set(expected));"
        "changed=sorted(k for k in set(expected)&set(actual) if expected[k]!=actual[k]);"
        "minimal=int(not missing and not extra and not changed);"
        "proxy=int(not any(k.upper().endswith('_PROXY') for k in os.environ));"
        "poison=int('SERENA_LIGHT_LIFECYCLE_POISON' not in os.environ);"
        "keys={'changed_keys':changed,'extra_keys':extra,'missing_keys':missing};"
        "status=subprocess.call(sys.argv[2:]);"
        f"sys.stderr.write('\\nAuthorization: Bearer {_SYNTHETIC_BEARER}\\n');"
        f"sys.stderr.write('password={_SYNTHETIC_PASSWORD}\\n');"
        f"sys.stderr.write('\\n{_ENVIRONMENT_MARKER} minimal=%d proxy=%d poison=%d %s\\n'"
        "% (minimal,proxy,poison,json.dumps(keys,sort_keys=True,separators=(',',':'))));"
        "sys.stderr.flush();"
        "sys.exit(status)"
    )


def _optional_bool(value: object, label: str) -> None:
    if value is not None and type(value) is not bool:
        raise ValueError(f"{label} must be a boolean or null")


def _non_negative_int(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class LifecycleScenarioEvidence:
    """A bounded protocol/process summary for exactly one lifecycle scenario."""

    name: str
    passed: bool
    elapsed_seconds: float
    attempt_count: int
    observed_error_code: int | None
    retry_disabled: bool | None
    diagnostics_mode: str | None
    diagnostics_observed: bool | None
    process_reaped: bool
    exit_status: int | None
    terminal_error_count: int
    cleanup_error_count: int
    proxy_rejected: bool | None
    minimal_environment_verified: bool | None
    redaction_verified: bool | None
    detail: str

    def __post_init__(self) -> None:
        if self.name not in LIFECYCLE_SCENARIOS:
            raise ValueError(f"LifecycleScenarioEvidence.name must be one of {list(LIFECYCLE_SCENARIOS)}")
        if type(self.passed) is not bool:
            raise ValueError("LifecycleScenarioEvidence.passed must be a boolean")
        if (
            isinstance(self.elapsed_seconds, bool)
            or not isinstance(self.elapsed_seconds, int | float)
            or not math.isfinite(self.elapsed_seconds)
            or self.elapsed_seconds < 0
        ):
            raise ValueError("LifecycleScenarioEvidence.elapsed_seconds must be finite and non-negative")
        _non_negative_int(self.attempt_count, "LifecycleScenarioEvidence.attempt_count")
        if self.observed_error_code is not None and (
            isinstance(self.observed_error_code, bool) or not isinstance(self.observed_error_code, int)
        ):
            raise ValueError("LifecycleScenarioEvidence.observed_error_code must be an integer or null")
        _optional_bool(self.retry_disabled, "LifecycleScenarioEvidence.retry_disabled")
        if self.diagnostics_mode is not None and self.diagnostics_mode not in DIAGNOSTICS_MODES:
            raise ValueError(
                "LifecycleScenarioEvidence.diagnostics_mode must be one of "
                f"{sorted(DIAGNOSTICS_MODES)} or null"
            )
        _optional_bool(self.diagnostics_observed, "LifecycleScenarioEvidence.diagnostics_observed")
        if type(self.process_reaped) is not bool:
            raise ValueError("LifecycleScenarioEvidence.process_reaped must be a boolean")
        if self.exit_status is not None and (
            isinstance(self.exit_status, bool) or not isinstance(self.exit_status, int)
        ):
            raise ValueError("LifecycleScenarioEvidence.exit_status must be an integer or null")
        _non_negative_int(self.terminal_error_count, "LifecycleScenarioEvidence.terminal_error_count")
        _non_negative_int(self.cleanup_error_count, "LifecycleScenarioEvidence.cleanup_error_count")
        _optional_bool(self.proxy_rejected, "LifecycleScenarioEvidence.proxy_rejected")
        _optional_bool(
            self.minimal_environment_verified,
            "LifecycleScenarioEvidence.minimal_environment_verified",
        )
        _optional_bool(self.redaction_verified, "LifecycleScenarioEvidence.redaction_verified")
        if not isinstance(self.detail, str) or len(self.detail) > _MAX_DETAIL_CHARS:
            raise ValueError(
                f"LifecycleScenarioEvidence.detail must be a string of at most {_MAX_DETAIL_CHARS} characters"
            )


@dataclass(frozen=True, slots=True)
class LifecycleBatteryRequest:
    """The already-frozen inputs for one candidate's lifecycle battery."""

    candidate: str
    spec: BackendProtocolSpec
    runtime: CandidateRuntime
    workspace_root: Path
    target: Path
    language_id: str
    diagnostics_mode: str

    def __post_init__(self) -> None:
        if self.candidate not in _CANDIDATES:
            raise ValueError(f"LifecycleBatteryRequest.candidate must be one of {sorted(_CANDIDATES)}")
        if not self.language_id:
            raise ValueError("LifecycleBatteryRequest.language_id must be non-empty")
        if self.diagnostics_mode not in DIAGNOSTICS_MODES:
            raise ValueError(
                f"LifecycleBatteryRequest.diagnostics_mode must be one of {sorted(DIAGNOSTICS_MODES)}"
            )


@dataclass(frozen=True, slots=True)
class LifecycleBatteryResult:
    lifecycle: LifecycleEvidence
    scenarios: tuple[LifecycleScenarioEvidence, ...]
    issues: tuple[str, ...]


class LifecycleScenarioExecutor(Protocol):
    """Injectable scenario boundary used by deterministic orchestration tests."""

    def execute(
        self,
        scenario: str,
        *,
        deadline: Deadline,
    ) -> LifecycleScenarioEvidence: ...


def run_lifecycle_battery(
    request: LifecycleBatteryRequest,
    *,
    deadline: Deadline,
    executor: LifecycleScenarioExecutor | None = None,
) -> LifecycleBatteryResult:
    """Run all seven lifecycle scenarios once and derive receipt-consumable evidence."""

    selected = executor or _RealLifecycleScenarioExecutor(request)
    scenarios: list[LifecycleScenarioEvidence] = []
    for scenario in LIFECYCLE_SCENARIOS:
        deadline.check(f"{request.candidate} lifecycle {scenario}")
        evidence = selected.execute(scenario, deadline=deadline)
        if evidence.name != scenario:
            raise ValueError(
                f"lifecycle executor returned evidence for {evidence.name!r} while running {scenario!r}"
            )
        scenarios.append(evidence)

    by_name = {scenario.name: scenario for scenario in scenarios}
    content_modified = by_name["content_modified"]
    request_cancelled = by_name["request_cancelled"]
    cold = by_name["cold_diagnostics"]
    environment = by_name["environment_redaction"]
    lifecycle = LifecycleEvidence(
        cold_readiness_seconds=cold.elapsed_seconds,
        diagnostics_mode=cold.diagnostics_mode or request.diagnostics_mode,
        content_modified_count=(
            content_modified.attempt_count
            if content_modified.observed_error_code == CONTENT_MODIFIED
            else 0
        ),
        request_cancelled_count=(
            request_cancelled.attempt_count
            if request_cancelled.observed_error_code == _REQUEST_CANCELLED
            else 0
        ),
        retry_seam_disabled=(
            content_modified.retry_disabled is True
            and request_cancelled.retry_disabled is True
        ),
        bounded_timeout_observed=by_name["bounded_timeout"].passed,
        crash_handled=by_name["crash"].passed,
        shutdown_clean=by_name["graceful_shutdown"].passed,
        cleanup_clean=all(
            scenario.process_reaped and scenario.cleanup_error_count == 0
            for scenario in scenarios
        ),
        proxy_rejected=environment.proxy_rejected is True,
        minimal_environment_verified=environment.minimal_environment_verified is True,
        redaction_verified=environment.redaction_verified is True,
    )
    issues = tuple(
        f"{scenario.name}: {scenario.detail or 'required lifecycle observation failed'}"
        for scenario in scenarios
        if not scenario.passed
    )
    return LifecycleBatteryResult(
        lifecycle=lifecycle,
        scenarios=tuple(scenarios),
        issues=issues,
    )


@dataclass(frozen=True, slots=True)
class _ProcessIdentity:
    pid: int
    process_group: int
    create_time: float


@dataclass(frozen=True, slots=True)
class _ProbeObservation:
    session: ProtocolSession[Any] | None
    process: _ProcessIdentity | None
    process_reaped: bool
    error: BaseException | None
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class _ColdResult:
    readiness_seconds: float
    symbol_count: int
    diagnostics_observed: bool


@dataclass(frozen=True, slots=True)
class _ErrorResult:
    attempts: int
    error_code: int | None
    retry_disabled: bool


class _RealLifecycleScenarioExecutor:
    def __init__(self, request: LifecycleBatteryRequest) -> None:
        if not isinstance(request.spec, BackendProtocolSpec):
            raise TypeError("real lifecycle execution requires BackendProtocolSpec")
        if not isinstance(request.runtime, CandidateRuntime):
            raise TypeError("real lifecycle execution requires CandidateRuntime")
        self._request = request

    def execute(
        self,
        scenario: str,
        *,
        deadline: Deadline,
    ) -> LifecycleScenarioEvidence:
        if scenario == "cold_diagnostics":
            return self._cold_diagnostics(deadline)
        if scenario == "content_modified":
            return self._injected_error(scenario, CONTENT_MODIFIED, deadline)
        if scenario == "request_cancelled":
            return self._injected_error(scenario, _REQUEST_CANCELLED, deadline)
        if scenario == "bounded_timeout":
            return self._bounded_timeout(deadline)
        if scenario == "crash":
            return self._crash(deadline)
        if scenario == "graceful_shutdown":
            return self._graceful_shutdown(deadline)
        if scenario == "environment_redaction":
            return self._environment_redaction(deadline)
        raise ValueError(f"unknown lifecycle scenario: {scenario}")

    def _run_probe(
        self,
        session: Callable[[SyncLspClient, RawLspProviders, _ProcessIdentity], Any],
        *,
        deadline: Deadline,
        spec: BackendProtocolSpec | None = None,
    ) -> _ProbeObservation:
        before = _direct_children()
        captured: list[_ProcessIdentity] = []
        started = deadline.elapsed()

        def observed_session(
            client: SyncLspClient,
            providers: RawLspProviders,
        ) -> Any:
            process = _discover_new_owned_child(before)
            captured.append(process)
            return session(client, providers, process)

        error: BaseException | None = None
        protocol_session: ProtocolSession[Any] | None = None
        try:
            protocol_session = run_protocol_probe(
                spec or self._request.spec,
                self._request.runtime,
                self._request.workspace_root,
                deadline=deadline,
                session=observed_session,
            )
        except DeadlineExceeded:
            raise
        except BaseException as probe_error:
            error = probe_error
            protocol_session = protocol_session_from_error(probe_error)
        process = captured[0] if len(captured) == 1 else None
        try:
            process_reaped = _wait_for_process_group_reaped(process, deadline)
        except DeadlineExceeded:
            raise
        except BaseException as census_error:
            process_reaped = False
            if error is None:
                error = census_error
        return _ProbeObservation(
            session=protocol_session,
            process=process,
            process_reaped=process_reaped,
            error=error,
            elapsed_seconds=max(0.0, deadline.elapsed() - started),
        )

    def _cold_diagnostics(self, deadline: Deadline) -> LifecycleScenarioEvidence:
        request = self._request
        source = request.target if request.target.is_absolute() else request.workspace_root / request.target
        source_text = read_stable_source_text(request.workspace_root, source, deadline=deadline)
        source_uri = source.as_uri()
        started = deadline.elapsed()
        diagnostics = threading.Event()
        original_notification_handler = request.spec.notification_handler

        def observe_notification(method: str, params: Any) -> None:
            if original_notification_handler is not None:
                original_notification_handler(method, params)
            if method != "textDocument/publishDiagnostics" or not isinstance(params, Mapping):
                return
            items = params.get("diagnostics")
            if params.get("uri") == source_uri and isinstance(items, Sequence) and not isinstance(
                items, str | bytes
            ):
                diagnostics.set()

        cold_spec = dataclass_replace(request.spec, notification_handler=observe_notification)

        def session(
            client: SyncLspClient,
            _providers: RawLspProviders,
            _process: _ProcessIdentity,
        ) -> _ColdResult:
            _notify_configuration(client, request)
            client.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": source_uri,
                        "languageId": request.language_id,
                        "version": 1,
                        "text": source_text,
                    }
                },
            )
            try:
                raw_symbols = client.request(
                    "textDocument/documentSymbol",
                    {"textDocument": {"uri": source_uri}},
                    timeout=deadline.remaining(),
                )
                symbol_count = (
                    len(raw_symbols)
                    if isinstance(raw_symbols, Sequence) and not isinstance(raw_symbols, str | bytes)
                    else 0
                )
                if request.diagnostics_mode == "pull":
                    raw_diagnostics = client.request(
                        "textDocument/diagnostic",
                        {"textDocument": {"uri": source_uri}},
                        timeout=deadline.remaining(),
                    )
                    diagnostics_observed = (
                        isinstance(raw_diagnostics, Mapping)
                        and raw_diagnostics.get("kind") in {"full", "unchanged"}
                    )
                else:
                    remaining = deadline.remaining()
                    if remaining <= 0.0:
                        deadline.check(f"{request.candidate} push diagnostics")
                    diagnostics_observed = diagnostics.wait(
                        timeout=min(_DIAGNOSTICS_WAIT_SECONDS, remaining)
                    )
                return _ColdResult(
                    readiness_seconds=max(0.0, deadline.elapsed() - started),
                    symbol_count=symbol_count,
                    diagnostics_observed=diagnostics_observed,
                )
            finally:
                client.notify("textDocument/didClose", {"textDocument": {"uri": source_uri}})

        observed = self._run_probe(session, deadline=deadline, spec=cold_spec)
        result = observed.session.result if observed.session is not None else None
        cold = result if isinstance(result, _ColdResult) else None
        cleanup_errors = _cleanup_error_count(observed.session)
        passed = (
            observed.error is None
            and cold is not None
            and cold.symbol_count > 0
            and cold.diagnostics_observed
            and observed.process_reaped
            and cleanup_errors == 0
        )
        return _scenario_evidence(
            name="cold_diagnostics",
            passed=passed,
            elapsed_seconds=(cold.readiness_seconds if cold is not None else observed.elapsed_seconds),
            diagnostics_mode=request.diagnostics_mode,
            diagnostics_observed=(cold.diagnostics_observed if cold is not None else False),
            observation=observed,
            detail=("" if passed else _failure_detail("cold readiness or diagnostics proof failed", observed)),
        )

    def _injected_error(
        self,
        scenario: str,
        error_code: int,
        deadline: Deadline,
    ) -> LifecycleScenarioEvidence:
        def session(
            client: SyncLspClient,
            _providers: RawLspProviders,
            _process: _ProcessIdentity,
        ) -> _ErrorResult:
            attempts = 0
            original_request_once = cast("Any", client)._request_once
            retry_disabled = cast("Any", client)._retry_methods == frozenset()

            def injected_once(method: str, params: Any, *, timeout: float | None) -> object:
                nonlocal attempts
                del params, timeout
                attempts += 1
                raise LspResponseError(error_code, f"injected lifecycle response for {method}")

            cast("Any", client)._request_once = injected_once
            observed_code: int | None = None
            try:
                try:
                    client.request(
                        "workspace/symbol",
                        {"query": "serena_light_lifecycle"},
                        timeout=deadline.remaining(),
                    )
                except LspResponseError as response_error:
                    observed_code = response_error.code
            finally:
                cast("Any", client)._request_once = original_request_once
            return _ErrorResult(
                attempts=attempts,
                error_code=observed_code,
                retry_disabled=retry_disabled,
            )

        observed = self._run_probe(session, deadline=deadline)
        result = observed.session.result if observed.session is not None else None
        injected = result if isinstance(result, _ErrorResult) else None
        cleanup_errors = _cleanup_error_count(observed.session)
        passed = (
            observed.error is None
            and injected is not None
            and injected.attempts == 1
            and injected.error_code == error_code
            and injected.retry_disabled
            and observed.process_reaped
            and cleanup_errors == 0
        )
        return _scenario_evidence(
            name=scenario,
            passed=passed,
            elapsed_seconds=observed.elapsed_seconds,
            attempt_count=(injected.attempts if injected is not None else 0),
            observed_error_code=(injected.error_code if injected is not None else None),
            retry_disabled=(injected.retry_disabled if injected is not None else False),
            observation=observed,
            detail=("" if passed else _failure_detail("one-attempt error observation failed", observed)),
        )

    def _bounded_timeout(self, deadline: Deadline) -> LifecycleScenarioEvidence:
        def session(
            client: SyncLspClient,
            _providers: RawLspProviders,
            _process: _ProcessIdentity,
        ) -> None:
            client.request(
                "workspace/symbol",
                {"query": "serena_light_lifecycle_timeout"},
                timeout=1e-9,
            )

        observed = self._run_probe(session, deadline=deadline)
        passed = (
            isinstance(observed.error, TimeoutError)
            and observed.process_reaped
            and _cleanup_error_count(observed.session) == 0
        )
        return _scenario_evidence(
            name="bounded_timeout",
            passed=passed,
            elapsed_seconds=observed.elapsed_seconds,
            observation=observed,
            detail=("" if passed else _failure_detail("typed request timeout was not observed", observed)),
        )

    def _crash(self, deadline: Deadline) -> LifecycleScenarioEvidence:
        def session(
            client: SyncLspClient,
            _providers: RawLspProviders,
            process: _ProcessIdentity,
        ) -> None:
            _signal_captured_process(process, signal.SIGKILL)
            _wait_until(lambda: not client.is_running, deadline, _PROCESS_REAP_SECONDS)
            client.request(
                "workspace/symbol",
                {"query": "serena_light_lifecycle_after_crash"},
                timeout=min(2.0, deadline.remaining()),
            )

        observed = self._run_probe(session, deadline=deadline)
        passed = (
            isinstance(observed.error, LspTransportClosed)
            and observed.process_reaped
            and observed.session is not None
            and observed.session.exit_status == -signal.SIGKILL
            and bool(observed.session.terminal_errors)
            and not observed.session.cleanup_errors
        )
        return _scenario_evidence(
            name="crash",
            passed=passed,
            elapsed_seconds=observed.elapsed_seconds,
            observation=observed,
            detail=("" if passed else _failure_detail("crash was not typed and fully reaped", observed)),
        )

    def _graceful_shutdown(self, deadline: Deadline) -> LifecycleScenarioEvidence:
        observed = self._run_probe(
            lambda _client, _providers, _process: "initialized",
            deadline=deadline,
        )
        passed = (
            observed.error is None
            and observed.session is not None
            and observed.session.result == "initialized"
            and observed.session.exit_status == 0
            and not observed.session.terminal_errors
            and not observed.session.cleanup_errors
            and observed.process_reaped
        )
        return _scenario_evidence(
            name="graceful_shutdown",
            passed=passed,
            elapsed_seconds=observed.elapsed_seconds,
            observation=observed,
            detail=("" if passed else _failure_detail("graceful shutdown did not exit cleanly", observed)),
        )

    def _environment_redaction(self, deadline: Deadline) -> LifecycleScenarioEvidence:
        request = self._request
        direct_spec = request.spec
        direct_build_command = direct_spec.build_command
        engine = direct_spec.engine(request.runtime)
        if engine.interpreter is None:
            raise ValueError(
                f"{request.candidate} lifecycle environment proof requires an engine-bound interpreter"
            )
        expected_environment = minimal_backend_environment(request.runtime, engine.interpreter)
        expected_json = json.dumps(expected_environment, sort_keys=True, separators=(",", ":"))
        wrapper = _environment_wrapper_program()

        def wrapped_command(runtime: CandidateRuntime) -> tuple[str, ...]:
            command = direct_build_command(runtime)
            return (
                str(runtime.python),
                "-I",
                "-c",
                wrapper,
                expected_json,
                *command,
            )

        wrapped_spec = dataclass_replace(direct_spec, build_command=wrapped_command)
        with _temporary_environment(_AMBIENT_POISON):
            observed = self._run_probe(
                lambda _client, _providers, _process: "initialized",
                deadline=deadline,
                spec=wrapped_spec,
            )
        stderr = observed.session.stderr_tail if observed.session is not None else ""
        minimal, proxy, poison = _require_minimal_environment_measurement(stderr)
        redacted = (
            _SYNTHETIC_BEARER not in stderr
            and _SYNTHETIC_PASSWORD not in stderr
            and stderr.count("<redacted>") >= 2
            and len(stderr) <= 1024
        )
        passed = (
            observed.error is None
            and observed.session is not None
            and observed.session.exit_status == 0
            and observed.process_reaped
            and not observed.session.cleanup_errors
            and minimal
            and proxy
            and poison
            and redacted
        )
        return _scenario_evidence(
            name="environment_redaction",
            passed=passed,
            elapsed_seconds=observed.elapsed_seconds,
            observation=observed,
            proxy_rejected=proxy and poison,
            minimal_environment_verified=minimal,
            redaction_verified=redacted,
            detail=(
                ""
                if passed
                else (
                    f"{_environment_marker_detail(stderr)}; "
                    f"{_failure_detail('environment or redaction proof failed', observed)}"
                )[:_MAX_DETAIL_CHARS]
            ),
        )


def _notify_configuration(client: SyncLspClient, request: LifecycleBatteryRequest) -> None:
    if request.candidate == "pyright":
        client.notify("workspace/didChangeConfiguration", {"settings": {}})
        return


def _scenario_evidence(
    *,
    name: str,
    passed: bool,
    elapsed_seconds: float,
    observation: _ProbeObservation,
    attempt_count: int = 0,
    observed_error_code: int | None = None,
    retry_disabled: bool | None = None,
    diagnostics_mode: str | None = None,
    diagnostics_observed: bool | None = None,
    proxy_rejected: bool | None = None,
    minimal_environment_verified: bool | None = None,
    redaction_verified: bool | None = None,
    detail: str = "",
) -> LifecycleScenarioEvidence:
    session = observation.session
    return LifecycleScenarioEvidence(
        name=name,
        passed=passed,
        elapsed_seconds=elapsed_seconds,
        attempt_count=attempt_count,
        observed_error_code=observed_error_code,
        retry_disabled=retry_disabled,
        diagnostics_mode=diagnostics_mode,
        diagnostics_observed=diagnostics_observed,
        process_reaped=observation.process_reaped,
        exit_status=(session.exit_status if session is not None else None),
        terminal_error_count=(len(session.terminal_errors) if session is not None else 0),
        cleanup_error_count=(len(session.cleanup_errors) if session is not None else 0),
        proxy_rejected=proxy_rejected,
        minimal_environment_verified=minimal_environment_verified,
        redaction_verified=redaction_verified,
        detail=detail[:_MAX_DETAIL_CHARS],
    )


def _failure_detail(prefix: str, observed: _ProbeObservation) -> str:
    error_name = type(observed.error).__name__ if observed.error is not None else "none"
    response_detail = ""
    if isinstance(observed.error, LspResponseError):
        response_message = redacted_evidence_text(observed.error.message)[:192]
        response_detail = f"; code={observed.error.code}; message={response_message}"
    session = observed.session
    terminal_count = len(session.terminal_errors) if session is not None else 0
    cleanup_count = len(session.cleanup_errors) if session is not None else 0
    exit_status = session.exit_status if session is not None else None
    return (
        f"{prefix}; error={error_name}{response_detail}; exit={exit_status}; "
        f"terminal_errors={terminal_count}; "
        f"cleanup_errors={cleanup_count}; process_reaped={observed.process_reaped}"
    )[:_MAX_DETAIL_CHARS]


def _cleanup_error_count(session: ProtocolSession[Any] | None) -> int:
    return len(session.cleanup_errors) if session is not None else 0


def _direct_children() -> frozenset[tuple[int, float]]:
    identities: set[tuple[int, float]] = set()
    try:
        children = psutil.Process(os.getpid()).children(recursive=False)
    except (psutil.AccessDenied, psutil.NoSuchProcess) as error:
        raise RuntimeError(f"cannot census evaluator child processes: {type(error).__name__}") from error
    for child in children:
        try:
            identities.add((child.pid, child.create_time()))
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as error:
            raise RuntimeError(
                f"cannot census evaluator child pid={child.pid}: AccessDenied"
            ) from error
    return frozenset(identities)


def _discover_new_owned_child(before: frozenset[tuple[int, float]]) -> _ProcessIdentity:
    candidates: list[_ProcessIdentity] = []
    try:
        children = psutil.Process(os.getpid()).children(recursive=False)
    except (psutil.AccessDenied, psutil.NoSuchProcess) as error:
        raise RuntimeError(f"cannot observe lifecycle child process: {type(error).__name__}") from error
    for child in children:
        try:
            identity = (child.pid, child.create_time())
            if identity in before:
                continue
            process_group = os.getpgid(child.pid)
        except (ProcessLookupError, psutil.NoSuchProcess):
            continue
        except (PermissionError, psutil.AccessDenied) as error:
            raise RuntimeError(
                f"cannot observe lifecycle child pid={child.pid}: {type(error).__name__}"
            ) from error
        except OSError as error:
            raise RuntimeError(
                f"cannot observe lifecycle child pid={child.pid}: {type(error).__name__}"
            ) from error
        if process_group != child.pid:
            raise RuntimeError(
                f"lifecycle candidate pid={child.pid} does not own its process group {process_group}"
            )
        candidates.append(
            _ProcessIdentity(
                pid=child.pid,
                process_group=process_group,
                create_time=identity[1],
            )
        )
    if len(candidates) != 1:
        raise RuntimeError(
            "lifecycle probe requires exactly one newly launched direct candidate child; "
            f"observed={len(candidates)}"
        )
    return candidates[0]


def _signal_captured_process(identity: _ProcessIdentity, signal_number: int) -> None:
    """Signal only the still-identical candidate-owned process group."""

    if identity.process_group != identity.pid:
        raise RuntimeError(
            f"captured candidate pid={identity.pid} does not own process group "
            f"{identity.process_group}"
        )
    try:
        process = psutil.Process(identity.pid)
        current_create_time = process.create_time()
    except (psutil.AccessDenied, psutil.NoSuchProcess) as error:
        raise RuntimeError(
            f"cannot re-prove captured candidate pid={identity.pid}: {type(error).__name__}"
        ) from error
    if current_create_time != identity.create_time:
        raise RuntimeError(
            f"captured candidate pid={identity.pid} identity changed before signal"
        )
    try:
        current_process_group = os.getpgid(identity.pid)
    except OSError as error:
        raise RuntimeError(
            f"cannot re-prove process group for captured candidate pid={identity.pid}: "
            f"{type(error).__name__}"
        ) from error
    if current_process_group != identity.process_group:
        raise RuntimeError(
            f"captured candidate pid={identity.pid} process group changed from "
            f"{identity.process_group} to {current_process_group}"
        )
    try:
        os.killpg(identity.process_group, signal_number)
    except OSError as error:
        raise RuntimeError(
            f"cannot signal captured candidate pid={identity.pid} group={identity.process_group}: "
            f"{type(error).__name__}"
        ) from error


def _same_process_alive(identity: _ProcessIdentity) -> bool:
    try:
        process = psutil.Process(identity.pid)
        return process.create_time() == identity.create_time and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied as error:
        raise RuntimeError(
            f"cannot census captured candidate pid={identity.pid}: AccessDenied"
        ) from error


def _live_process_group_members(process_group: int) -> tuple[int, ...]:
    members: list[int] = []
    for process in psutil.process_iter(["pid", "status"]):
        try:
            if process.info["status"] == psutil.STATUS_ZOMBIE:
                continue
            if os.getpgid(process.pid) == process_group:
                members.append(process.pid)
        except (ProcessLookupError, psutil.NoSuchProcess):
            continue
        except (PermissionError, psutil.AccessDenied) as error:
            raise RuntimeError(
                f"cannot census process group {process_group}: {type(error).__name__}"
            ) from error
        except OSError as error:
            raise RuntimeError(
                f"cannot census process group {process_group}: {type(error).__name__}"
            ) from error
    return tuple(sorted(members))


def _wait_for_process_group_reaped(
    identity: _ProcessIdentity | None,
    deadline: Deadline,
) -> bool:
    if identity is None:
        return False
    wait_seconds = min(_PROCESS_REAP_SECONDS, deadline.remaining())
    end = time.monotonic() + wait_seconds
    while time.monotonic() < end:
        if not _same_process_alive(identity) and not _live_process_group_members(identity.process_group):
            return True
        time.sleep(0.02)
    return not _same_process_alive(identity) and not _live_process_group_members(identity.process_group)


def _wait_until(predicate: Callable[[], bool], deadline: Deadline, maximum_seconds: float) -> bool:
    end = time.monotonic() + min(maximum_seconds, deadline.remaining())
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@contextmanager
def _temporary_environment(values: Mapping[str, str]) -> Iterator[None]:
    with _ENVIRONMENT_LOCK:
        previous = {key: os.environ.get(key) for key in values}
        try:
            os.environ.update(values)
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ.update({key: value})
