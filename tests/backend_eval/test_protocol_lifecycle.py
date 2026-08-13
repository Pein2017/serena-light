"""Consumable lifecycle evidence for the Phase 2 protocol orchestrator."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

import scripts.backend_eval.protocol_lifecycle as lifecycle_module
from scripts.backend_eval.process import Deadline, monotonic_clock
from scripts.backend_eval.protocol import BackendProtocolSpec
from scripts.backend_eval.protocol_lifecycle import (
    LIFECYCLE_SCENARIOS,
    LifecycleBatteryRequest,
    LifecycleScenarioEvidence,
    run_lifecycle_battery,
)
from scripts.backend_eval.pyright_probe import _prepared_candidate_runtime, pyright_protocol_spec
from scripts.backend_eval.runtime import CandidateRuntime
from serena_light.lsp.client import LspResponseError
from serena_light.lsp.pyright import PyrightFacts
from serena_light.workspace.identity import MS_INTERPRETER

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MS_SWIFT = Path("/data/ms-swift")
_TARGET = Path("swift/infer_engine/lmdeploy_engine.py")


class _LiteralExecutor:
    def __init__(self, evidence: dict[str, LifecycleScenarioEvidence]) -> None:
        self.evidence = evidence
        self.calls: list[str] = []

    def execute(
        self,
        scenario: str,
        *,
        deadline: Deadline,
    ) -> LifecycleScenarioEvidence:
        deadline.check(f"fake {scenario}")
        self.calls.append(scenario)
        return self.evidence[scenario]


def _passing_scenarios() -> dict[str, LifecycleScenarioEvidence]:
    common = LifecycleScenarioEvidence(
        name="graceful_shutdown",
        passed=True,
        elapsed_seconds=0.25,
        attempt_count=0,
        observed_error_code=None,
        retry_disabled=None,
        diagnostics_mode=None,
        diagnostics_observed=None,
        process_reaped=True,
        exit_status=0,
        terminal_error_count=0,
        cleanup_error_count=0,
        proxy_rejected=None,
        minimal_environment_verified=None,
        redaction_verified=None,
        detail="",
    )
    return {
        "cold_diagnostics": replace(
            common,
            name="cold_diagnostics",
            elapsed_seconds=1.25,
            diagnostics_mode="push",
            diagnostics_observed=True,
        ),
        "content_modified": replace(
            common,
            name="content_modified",
            attempt_count=1,
            observed_error_code=-32801,
            retry_disabled=True,
        ),
        "request_cancelled": replace(
            common,
            name="request_cancelled",
            attempt_count=1,
            observed_error_code=-32800,
            retry_disabled=True,
        ),
        "bounded_timeout": replace(
            common,
            name="bounded_timeout",
            exit_status=-15,
        ),
        "crash": replace(
            common,
            name="crash",
            exit_status=-9,
            terminal_error_count=1,
        ),
        "graceful_shutdown": common,
        "environment_redaction": replace(
            common,
            name="environment_redaction",
            proxy_rejected=True,
            minimal_environment_verified=True,
            redaction_verified=True,
        ),
    }


def test_candidate_eliminating_lifecycle_detail_retains_bounded_redacted_lsp_error() -> None:
    secret = "lifecycle-secret"
    observation = lifecycle_module._ProbeObservation(
        session=None,
        process=None,
        process_reaped=True,
        error=LspResponseError(
            -32800,
            f"password={secret} request cancelled " + "x" * 5000,
        ),
        elapsed_seconds=0.25,
    )

    detail = lifecycle_module._failure_detail(
        "cold readiness or diagnostics proof failed",
        observation,
    )

    assert "code=-32800" in detail
    assert "message=" in detail
    assert secret not in detail
    assert "<redacted>" in detail
    assert len(detail) <= 512


def _fake_request() -> LifecycleBatteryRequest:
    return LifecycleBatteryRequest(
        candidate="pyright",
        spec=cast("BackendProtocolSpec", object()),
        runtime=cast("CandidateRuntime", object()),
        workspace_root=Path("/workspace"),
        target=Path("sample.py"),
        language_id="python",
        diagnostics_mode="push",
    )


def test_battery_runs_each_fixed_scenario_once_and_derives_lifecycle_evidence() -> None:
    executor = _LiteralExecutor(_passing_scenarios())

    result = run_lifecycle_battery(
        _fake_request(),
        deadline=Deadline.start(monotonic_clock, 10.0),
        executor=executor,
    )

    assert tuple(executor.calls) == LIFECYCLE_SCENARIOS
    assert tuple(item.name for item in result.scenarios) == LIFECYCLE_SCENARIOS
    assert result.lifecycle.cold_readiness_seconds == 1.25
    assert result.lifecycle.diagnostics_mode == "push"
    assert result.lifecycle.content_modified_count == 1
    assert result.lifecycle.request_cancelled_count == 1
    assert result.lifecycle.retry_seam_disabled is True
    assert result.lifecycle.bounded_timeout_observed is True
    assert result.lifecycle.crash_handled is True
    assert result.lifecycle.shutdown_clean is True
    assert result.lifecycle.cleanup_clean is True
    assert result.lifecycle.proxy_rejected is True
    assert result.lifecycle.minimal_environment_verified is True
    assert result.lifecycle.redaction_verified is True
    assert result.issues == ()


def test_failed_observation_cannot_be_promoted_to_lifecycle_success() -> None:
    evidence = _passing_scenarios()
    evidence["bounded_timeout"] = replace(
        evidence["bounded_timeout"],
        passed=False,
        detail="request returned instead of timing out",
    )
    executor = _LiteralExecutor(evidence)

    result = run_lifecycle_battery(
        _fake_request(),
        deadline=Deadline.start(monotonic_clock, 10.0),
        executor=executor,
    )

    assert result.lifecycle.bounded_timeout_observed is False
    assert result.issues == (
        "bounded_timeout: request returned instead of timing out",
    )


def test_executor_cannot_substitute_evidence_for_another_scenario() -> None:
    evidence = _passing_scenarios()
    evidence["crash"] = replace(evidence["crash"], name="graceful_shutdown")

    with pytest.raises(ValueError, match="returned evidence for"):
        run_lifecycle_battery(
            _fake_request(),
            deadline=Deadline.start(monotonic_clock, 10.0),
            executor=_LiteralExecutor(evidence),
        )


def test_scenario_detail_is_bounded_before_it_can_enter_a_receipt() -> None:
    with pytest.raises(ValueError, match="detail"):
        LifecycleScenarioEvidence(
            name="crash",
            passed=False,
            elapsed_seconds=0.0,
            attempt_count=0,
            observed_error_code=None,
            retry_disabled=None,
            diagnostics_mode=None,
            diagnostics_observed=None,
            process_reaped=False,
            exit_status=None,
            terminal_error_count=0,
            cleanup_error_count=0,
            proxy_rejected=None,
            minimal_environment_verified=None,
            redaction_verified=None,
            detail="x" * 513,
        )


def test_environment_marker_retains_key_level_mismatch_cause_without_values() -> None:
    secret = "super-secret-value"
    stderr = (
        "noise\n"
        "SERENA_LIGHT_LIFECYCLE_ENV minimal=0 proxy=1 poison=1 "
        '{"changed_keys":["PATH"],"extra_keys":["FOREIGN"],'
        '"missing_keys":["HOME"]}\n'
        f"password={secret}\n"
    )

    detail = lifecycle_module._environment_marker_detail(stderr)

    assert "minimal=0" in detail
    assert "changed_keys" in detail
    assert "PATH" in detail
    assert "extra_keys" in detail
    assert "FOREIGN" in detail
    assert "missing_keys" in detail
    assert "HOME" in detail
    assert secret not in detail
    assert len(detail) <= 512


def test_environment_mismatch_is_phase_infrastructure_not_candidate_evidence() -> None:
    stderr = (
        "SERENA_LIGHT_LIFECYCLE_ENV minimal=0 proxy=1 poison=1 "
        '{"changed_keys":["PATH"],"extra_keys":[],"missing_keys":[]}\n'
    )

    with pytest.raises(
        lifecycle_module.LifecycleInfrastructureError,
        match="changed_keys.*PATH",
    ):
        lifecycle_module._require_minimal_environment_measurement(stderr)


def test_missing_environment_measurement_is_phase_infrastructure() -> None:
    with pytest.raises(
        lifecycle_module.LifecycleInfrastructureError,
        match="marker unavailable",
    ):
        lifecycle_module._require_minimal_environment_measurement("candidate stderr only")


def test_environment_measurement_survives_more_than_the_persisted_stderr_tail() -> None:
    expected = {
        "HOME": "/service/home",
        "PATH": "/service/bin",
    }
    child = "import sys;sys.stderr.write('candidate-noise-' * 256)"

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            lifecycle_module._environment_wrapper_program(),
            json.dumps(expected, sort_keys=True, separators=(",", ":")),
            sys.executable,
            "-I",
            "-c",
            child,
        ],
        env=expected,
        capture_output=True,
        text=True,
        timeout=10.0,
        check=True,
    )

    persisted_tail = result.stderr[-1024:]
    assert "candidate-noise" in persisted_tail
    assert lifecycle_module._require_minimal_environment_measurement(persisted_tail) == (
        True,
        True,
        True,
    )


class _FakeProcess:
    def __init__(self, pid: int, create_time: float) -> None:
        self.pid = pid
        self._create_time = create_time

    def create_time(self) -> float:
        return self._create_time


def test_candidate_signal_reproves_exact_pid_create_time_and_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = lifecycle_module._ProcessIdentity(
        pid=43121,
        process_group=43121,
        create_time=17.5,
    )
    process = _FakeProcess(identity.pid, identity.create_time)
    process_requests: list[int] = []
    signals: list[tuple[int, int]] = []

    def observed_process(pid: int) -> _FakeProcess:
        process_requests.append(pid)
        return process

    monkeypatch.setattr(lifecycle_module.psutil, "Process", observed_process)
    monkeypatch.setattr(lifecycle_module.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(
        lifecycle_module.os,
        "killpg",
        lambda process_group, signal_number: signals.append((process_group, signal_number)),
    )

    lifecycle_module._signal_captured_process(identity, signal.SIGKILL)

    assert process_requests == [identity.pid]
    assert signals == [(identity.process_group, signal.SIGKILL)]

    process._create_time += 1.0
    with pytest.raises(RuntimeError, match="identity changed"):
        lifecycle_module._signal_captured_process(identity, signal.SIGKILL)
    assert signals == [(identity.process_group, signal.SIGKILL)]

    process._create_time = identity.create_time
    monkeypatch.setattr(lifecycle_module.os, "getpgid", lambda _pid: identity.process_group + 1)
    with pytest.raises(RuntimeError, match="process group changed"):
        lifecycle_module._signal_captured_process(identity, signal.SIGKILL)
    assert signals == [(identity.process_group, signal.SIGKILL)]


@pytest.mark.parametrize("children", [(), ((43121, 17.5), (43122, 18.5))])
def test_candidate_discovery_fails_closed_on_missing_or_extra_children(
    monkeypatch: pytest.MonkeyPatch,
    children: tuple[tuple[int, float], ...],
) -> None:
    processes = tuple(_FakeProcess(pid, create_time) for pid, create_time in children)

    class _FakeParent:
        def children(self, *, recursive: bool) -> tuple[_FakeProcess, ...]:
            assert recursive is False
            return processes

    monkeypatch.setattr(lifecycle_module.psutil, "Process", lambda _pid: _FakeParent())
    monkeypatch.setattr(lifecycle_module.os, "getpgid", lambda pid: pid)

    with pytest.raises(RuntimeError, match=f"observed={len(children)}"):
        lifecycle_module._discover_new_owned_child(frozenset())


@pytest.mark.timeout(240)
@pytest.mark.external_repo(
    root=str(_MS_SWIFT),
    snapshot_env="SERENA_LIGHT_MS_SWIFT_SNAPSHOT",
)
def test_real_locked_pyright_battery_produces_complete_cleanup_evidence() -> None:
    runtime = _prepared_candidate_runtime()
    facts = PyrightFacts.locked(root=_REPO_ROOT, interpreter=MS_INTERPRETER)
    request = LifecycleBatteryRequest(
        candidate="pyright",
        spec=pyright_protocol_spec(runtime, facts, production_root=_REPO_ROOT),
        runtime=runtime,
        workspace_root=_MS_SWIFT,
        target=_TARGET,
        language_id=facts.language_id,
        diagnostics_mode="push",
    )

    result = run_lifecycle_battery(
        request,
        deadline=Deadline.start(monotonic_clock, 210.0),
    )

    assert tuple(item.name for item in result.scenarios) == LIFECYCLE_SCENARIOS
    assert all(item.process_reaped for item in result.scenarios)
    assert result.lifecycle.cold_readiness_seconds > 0.0
    assert result.lifecycle.content_modified_count == 1
    assert result.lifecycle.request_cancelled_count == 1
    assert result.lifecycle.retry_seam_disabled is True
    assert result.lifecycle.bounded_timeout_observed is True
    assert result.lifecycle.crash_handled is True
    assert result.lifecycle.shutdown_clean is True
    assert result.lifecycle.cleanup_clean is True
    assert result.lifecycle.proxy_rejected is True
    assert result.lifecycle.minimal_environment_verified is True
    assert result.lifecycle.redaction_verified is True
    assert result.issues == ()
