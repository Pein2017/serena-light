from __future__ import annotations

import json
from pathlib import Path

import pytest

from serena_light.lsp.adapter import AdapterError, AdapterErrorCode
from serena_light.lsp.executor import ExecutorBusyError
from serena_light.tools.envelopes import (
    ENVELOPE_SCHEMA,
    AdapterMetadata,
    ErrorCode,
    ErrorEnvelope,
    GenerationMetadata,
    RetryMetadata,
    SuccessEnvelope,
    TruncationMetadata,
    WorkspaceMetadata,
    from_adapter_cooldown,
    from_adapter_error,
    from_executor_busy,
    from_readiness_result,
    from_timeout,
    from_workspace_error,
)
from serena_light.workspace.identity import (
    WorkspaceError,
    WorkspaceErrorCode,
    WorkspaceErrorData,
    WorkspaceIdentity,
    WorkspaceKind,
)
from serena_light.workspace.scope import (
    ReadinessCode,
    ReadinessResult,
    ReadinessScope,
)
from serena_light.workspace.scope import (
    RetryMetadata as ScopeRetry,
)


def test_success_serialization_snapshot_and_json_roundtrip() -> None:
    envelope = SuccessEnvelope(
        {"symbols": ["A"]},
        WorkspaceMetadata("/data/repo", "git", "/data/repo/src"),
        AdapterMetadata("pyright", "python"),
        GenerationMetadata(trust=1, program=2, document=3, index=4, scope="configured_program"),
        TruncationMetadata(True, 2),
    )

    assert envelope.to_dict() == {
        "ok": True,
        "data": {"symbols": ["A"]},
        "workspace": {"root": "/data/repo", "kind": "git", "working_subdirectory": "/data/repo/src"},
        "adapter": {"name": "pyright", "language": "python"},
        "generations": {"trust": 1, "program": 2, "document": 3, "index": 4, "scope": "configured_program"},
        "truncation": {"truncated": True, "omitted_count": 2},
    }
    assert json.loads(envelope.to_json()) == envelope.to_dict()
    assert ENVELOPE_SCHEMA["oneOf"][0]["required"] == ["ok", "data"]


@pytest.mark.parametrize("code", list(ErrorCode))
def test_each_error_code_has_stable_shape(code: ErrorCode) -> None:
    value = ErrorEnvelope(code).to_dict()
    assert value["ok"] is False
    assert value["error"] == {
        "code": code.value,
        "message": ErrorEnvelope(code).message,
        "retry": None,
        "details": {},
    }
    assert json.loads(ErrorEnvelope(code).to_json()) == value


def test_error_retry_and_details_are_typed_and_immutable() -> None:
    envelope = ErrorEnvelope(
        ErrorCode.NOT_READY,
        retry=RetryMetadata(
            True,
            retry_after_seconds=0.1,
            target_generation=3,
            observed_generation=2,
        ),
        details={"scope": "configured_program", "paths": ["src/a.py"]},
    )
    assert envelope.to_dict()["error"] == {
        "code": "NOT_READY",
        "message": "requested state is not ready",
        "retry": {
            "retryable": True,
            "retry_after_seconds": 0.1,
            "target_generation": 3,
            "observed_generation": 2,
        },
        "details": {"scope": "configured_program", "paths": ["src/a.py"]},
    }
    with pytest.raises(TypeError):
        envelope.details["other"] = "no"  # type: ignore[index]


def test_unknown_code_and_non_json_values_fail_fast() -> None:
    with pytest.raises(TypeError, match="ErrorCode"):
        ErrorEnvelope("UNKNOWN")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="JSON"):
        SuccessEnvelope({"path": Path("not-json")})
    with pytest.raises(TypeError, match="exceptions"):
        ErrorEnvelope(ErrorCode.INVALID_INPUT, details={"error": ValueError("bearer secret")})
    with pytest.raises(ValueError, match="fixed"):
        ErrorEnvelope(ErrorCode.INVALID_INPUT, message="token=secret")
    with pytest.raises(ValueError, match="secret-bearing"):
        ErrorEnvelope(ErrorCode.INVALID_INPUT, details={"bearer": "secret"})
    with pytest.raises(ValueError, match="non-finite"):
        SuccessEnvelope({"elapsed": float("inf")})


def test_workspace_conversion_redacts_original_exception_message() -> None:
    identity = WorkspaceIdentity(Path("/data/repo"), WorkspaceKind.GIT, Path("/data/repo"))
    exc = WorkspaceError(
        WorkspaceErrorData(
            WorkspaceErrorCode.OUT_OF_WORKSPACE,
            "bearer=top-secret and repr(Exception('secret'))",
            current_identity=identity,
            activation_hint=Path("/data/other"),
            path=Path("/data/other/file.py"),
        )
    )
    serialized = from_workspace_error(exc).to_dict()
    assert serialized["error"]["code"] == "OUT_OF_WORKSPACE"
    assert "secret" not in json.dumps(serialized)
    assert serialized["workspace"]["root"] == "/data/repo"


def test_current_domain_conversions() -> None:
    not_ready = ReadinessResult(
        ReadinessCode.NOT_READY,
        ReadinessScope.CONFIGURED_PROGRAM,
        False,
        None,
        3,
        2,
        ScopeRetry(True, 0.2, 0.1, 1.0, 3, 2),
    )
    readiness = from_readiness_result(not_ready).to_dict()
    assert readiness["error"]["code"] == "NOT_READY"
    assert readiness["error"]["retry"]["target_generation"] == 3
    busy = from_executor_busy(ExecutorBusyError("queue has bearer=secret")).to_dict()
    assert busy["error"]["code"] == "BUSY"
    cooldown = from_adapter_error(
        AdapterError(AdapterErrorCode.COOLDOWN, "token=secret", retry_after_seconds=4)
    ).to_dict()
    assert cooldown["error"]["code"] == "COOLDOWN"
    assert cooldown["error"]["retry"] == {"retryable": True, "retry_after_seconds": 4}
    assert from_adapter_cooldown(
        AdapterError(AdapterErrorCode.COOLDOWN, "token=secret", retry_after_seconds=4)
    ).to_dict() == cooldown
    assert from_timeout(TimeoutError("token=secret"), timeout_seconds=1).to_dict()["error"] == {
        "code": "TIMED_OUT",
        "message": "operation timed out",
        "retry": {"retryable": True, "timeout_seconds": 1},
        "details": {},
    }
