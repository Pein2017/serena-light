"""Stable, transport-neutral JSON envelopes for serena-light tools.

The tool layer owns this module so workspace and adapter code can remain
independent of transport formatting.  Conversion helpers import those domains
only while converting an already-raised value; the module has no import-time
dependency on the daemon, workspace, or adapter packages.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, cast

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]


class ErrorCode(StrEnum):
    """The complete public error vocabulary for v1 tool responses."""

    INVALID_PATH = "INVALID_PATH"
    UNTRUSTED_ROOT = "UNTRUSTED_ROOT"
    INVALID_INPUT = "INVALID_INPUT"
    OUT_OF_WORKSPACE = "OUT_OF_WORKSPACE"
    SCOPE_INCOMPATIBLE = "SCOPE_INCOMPATIBLE"
    NOT_READY = "NOT_READY"
    UNSUPPORTED = "UNSUPPORTED"
    SYMBOL_NOT_FOUND = "SYMBOL_NOT_FOUND"
    AMBIGUOUS_SYMBOL = "AMBIGUOUS_SYMBOL"
    STALE_HASH = "STALE_HASH"
    READ_ONLY_ROOT = "READ_ONLY_ROOT"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    BUSY = "BUSY"
    TIMED_OUT = "TIMED_OUT"
    COOLDOWN = "COOLDOWN"
    UNCERTAIN = "UNCERTAIN"


_MESSAGES: Mapping[ErrorCode, str] = MappingProxyType(
    {
        ErrorCode.INVALID_PATH: "path is invalid",
        ErrorCode.UNTRUSTED_ROOT: "path is outside trusted roots",
        ErrorCode.INVALID_INPUT: "input is invalid",
        ErrorCode.OUT_OF_WORKSPACE: "path is outside the active workspace",
        ErrorCode.SCOPE_INCOMPATIBLE: "native program is incompatible with workspace trust",
        ErrorCode.NOT_READY: "requested state is not ready",
        ErrorCode.UNSUPPORTED: "operation is unsupported",
        ErrorCode.SYMBOL_NOT_FOUND: "symbol was not found",
        ErrorCode.AMBIGUOUS_SYMBOL: "symbol selection is ambiguous",
        ErrorCode.STALE_HASH: "file content no longer matches expected hash",
        ErrorCode.READ_ONLY_ROOT: "path is in a read-only root",
        ErrorCode.LEASE_EXPIRED: "workspace lease has expired",
        ErrorCode.BUSY: "workspace executor is busy",
        ErrorCode.TIMED_OUT: "operation timed out",
        ErrorCode.COOLDOWN: "adapter is in cooldown",
        ErrorCode.UNCERTAIN: "operation outcome is uncertain",
    }
)
_SECRET_KEY_FRAGMENTS = ("authorization", "bearer", "cookie", "password", "secret", "token")


@dataclass(frozen=True, slots=True)
class WorkspaceMetadata:
    root: str
    kind: str
    working_subdirectory: str

    def __post_init__(self) -> None:
        for value in (self.root, self.working_subdirectory):
            if not value.startswith("/"):
                raise ValueError("workspace metadata paths must be absolute")

    def to_dict(self) -> dict[str, str]:
        return {"root": self.root, "kind": self.kind, "working_subdirectory": self.working_subdirectory}


@dataclass(frozen=True, slots=True)
class AdapterMetadata:
    name: str
    language: str | None = None

    def to_dict(self) -> dict[str, str]:
        data = {"name": self.name}
        if self.language is not None:
            data["language"] = self.language
        return data


@dataclass(frozen=True, slots=True)
class GenerationMetadata:
    trust: int | None = None
    program: int | None = None
    document: int | None = None
    index: int | None = None
    scope: str | None = None

    def __post_init__(self) -> None:
        for generation in (self.trust, self.program, self.document, self.index):
            if generation is not None and generation < 0:
                raise ValueError("generation values must be non-negative")

    def to_dict(self) -> dict[str, JsonValue]:
        return {key: value for key, value in self.__dict_items() if value is not None}

    def __dict_items(self) -> tuple[tuple[str, JsonValue | None], ...]:
        return (
            ("trust", self.trust),
            ("program", self.program),
            ("document", self.document),
            ("index", self.index),
            ("scope", self.scope),
        )


@dataclass(frozen=True, slots=True)
class TruncationMetadata:
    truncated: bool
    omitted_count: int = 0

    def __post_init__(self) -> None:
        if self.omitted_count < 0 or (not self.truncated and self.omitted_count != 0):
            raise ValueError("truncation metadata is inconsistent")

    def to_dict(self) -> dict[str, JsonValue]:
        return {"truncated": self.truncated, "omitted_count": self.omitted_count}


@dataclass(frozen=True, slots=True)
class RetryMetadata:
    retryable: bool
    retry_after_seconds: float | None = None
    waited_seconds: float | None = None
    timeout_seconds: float | None = None
    target_generation: int | None = None
    observed_generation: int | None = None

    def __post_init__(self) -> None:
        for value in (self.retry_after_seconds, self.waited_seconds, self.timeout_seconds):
            if value is not None and value < 0:
                raise ValueError("retry durations must be non-negative")
        for value in (self.target_generation, self.observed_generation):
            if value is not None and value < 0:
                raise ValueError("retry generations must be non-negative")

    def to_dict(self) -> dict[str, JsonValue]:
        values: tuple[tuple[str, JsonValue | None], ...] = (
            ("retryable", self.retryable),
            ("retry_after_seconds", self.retry_after_seconds),
            ("waited_seconds", self.waited_seconds),
            ("timeout_seconds", self.timeout_seconds),
            ("target_generation", self.target_generation),
            ("observed_generation", self.observed_generation),
        )
        return {key: value for key, value in values if value is not None}


def _json_value(value: Any, *, field_name: str = "value") -> JsonValue:
    """Copy a JSON value, rejecting exception objects and non-finite numbers."""

    if value is None or isinstance(value, bool | str | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} must not contain non-finite floats")
        return value
    if isinstance(value, Exception):
        raise TypeError(f"{field_name} must not contain exceptions")
    if isinstance(value, Mapping):
        copied: dict[str, JsonValue] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{field_name} mapping keys must be strings")
            if any(fragment in key.lower() for fragment in _SECRET_KEY_FRAGMENTS):
                raise ValueError(f"{field_name} must not contain secret-bearing keys")
            copied[key] = _json_value(nested, field_name=f"{field_name}.{key}")
        return MappingProxyType(copied)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return tuple(_json_value(nested, field_name=field_name) for nested in value)
    raise TypeError(f"{field_name} must contain only JSON values")


def _plain_json(value: JsonValue) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_json(cast(JsonValue, nested)) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(nested) for nested in value]
    return value


@dataclass(frozen=True, slots=True)
class SuccessEnvelope:
    data: Any
    workspace: WorkspaceMetadata | None = None
    adapter: AdapterMetadata | None = None
    generations: GenerationMetadata | None = None
    truncation: TruncationMetadata | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", _json_value(self.data, field_name="data"))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": True, "data": _plain_json(self.data)}
        if self.workspace is not None:
            result["workspace"] = self.workspace.to_dict()
        if self.adapter is not None:
            result["adapter"] = self.adapter.to_dict()
        if self.generations is not None:
            result["generations"] = self.generations.to_dict()
        if self.truncation is not None:
            result["truncation"] = self.truncation.to_dict()
        return result

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True, slots=True)
class ErrorEnvelope:
    code: ErrorCode
    message: str | None = None
    retry: RetryMetadata | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
    workspace: WorkspaceMetadata | None = None
    adapter: AdapterMetadata | None = None
    generations: GenerationMetadata | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, ErrorCode):
            raise TypeError("error code must be an ErrorCode")
        canonical_message = _MESSAGES[self.code]
        if self.message is not None and self.message != canonical_message:
            raise ValueError("error messages are fixed by error code")
        message = canonical_message
        if not message or "\n" in message or "\r" in message:
            raise ValueError("error message must be one non-empty line")
        # The public message is deliberately supplied only by this module or a
        # trusted tool author.  Domain exception strings never enter conversion.
        object.__setattr__(self, "message", message)
        copied = _json_value(self.details, field_name="details")
        if not isinstance(copied, Mapping):  # pragma: no cover - type guard
            raise TypeError("error details must be a mapping")
        object.__setattr__(self, "details", copied)

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "retry": None,
            "details": _plain_json(self.details),
        }
        if self.retry is not None:
            error["retry"] = self.retry.to_dict()
        result: dict[str, Any] = {"ok": False, "error": error}
        if self.workspace is not None:
            result["workspace"] = self.workspace.to_dict()
        if self.adapter is not None:
            result["adapter"] = self.adapter.to_dict()
        if self.generations is not None:
            result["generations"] = self.generations.to_dict()
        return result

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)


type ToolEnvelope = SuccessEnvelope | ErrorEnvelope


def success(
    data: JsonValue,
    *,
    workspace: WorkspaceMetadata | None = None,
    adapter: AdapterMetadata | None = None,
    generations: GenerationMetadata | None = None,
    truncation: TruncationMetadata | None = None,
) -> SuccessEnvelope:
    return SuccessEnvelope(data, workspace, adapter, generations, truncation)


def error(
    code: ErrorCode,
    *,
    retry: RetryMetadata | None = None,
    details: Mapping[str, Any] | None = None,
    workspace: WorkspaceMetadata | None = None,
    adapter: AdapterMetadata | None = None,
    generations: GenerationMetadata | None = None,
) -> ErrorEnvelope:
    return ErrorEnvelope(
        code,
        retry=retry,
        details={} if details is None else details,
        workspace=workspace,
        adapter=adapter,
        generations=generations,
    )


def workspace_metadata(identity: Any) -> WorkspaceMetadata:
    """Serialize a ``WorkspaceIdentity`` structurally without importing it."""

    return WorkspaceMetadata(
        root=str(identity.root),
        kind=str(identity.kind),
        working_subdirectory=str(identity.working_subdirectory),
    )


def from_workspace_error(exc: Exception) -> ErrorEnvelope:
    """Convert current workspace failures without exposing exception text."""

    from serena_light.workspace.identity import WorkspaceError, WorkspaceErrorCode

    if not isinstance(exc, WorkspaceError):
        raise TypeError("expected WorkspaceError")
    code = ErrorCode(WorkspaceErrorCode(exc.data.code).value)
    details: dict[str, JsonValue] = {}
    if exc.data.path is not None:
        details["path"] = str(exc.data.path)
    if exc.data.activation_hint is not None:
        details["activation_hint"] = str(exc.data.activation_hint)
    workspace = workspace_metadata(exc.data.current_identity) if exc.data.current_identity is not None else None
    return error(code, details=details, workspace=workspace)


def scope_error_details(scope_error: Any) -> dict[str, JsonValue]:
    """Render already-owned projection evidence for a `SCOPE_INCOMPATIBLE` failure.

    Reuses exactly the fields the projection already computed -- language,
    project kind, selected native config, and the bounded outside-trust
    difference set -- so no conversion site recomputes or reruns a probe.
    """

    from serena_light.workspace.scope import ScopeError

    if not isinstance(scope_error, ScopeError):
        raise TypeError("expected ScopeError")
    details: dict[str, JsonValue] = {
        "language": scope_error.language.value,
        "project_kind": scope_error.project_kind.value,
        "configured_program_outside_trust": scope_error.configured_program_outside_trust,
    }
    if scope_error.selected_config_path is not None:
        details["selected_config_path"] = scope_error.selected_config_path
    return details


def from_readiness_result(
    result: Any,
    *,
    workspace: WorkspaceMetadata | None = None,
    adapter: AdapterMetadata | None = None,
) -> ToolEnvelope:
    """Convert scope readiness into either a ready success or typed failure."""

    from serena_light.workspace.scope import ReadinessCode, ReadinessResult

    if not isinstance(result, ReadinessResult):
        raise TypeError("expected ReadinessResult")
    generations = GenerationMetadata(
        program=result.target_generation,
        index=result.observed_generation,
        scope=result.scope.value,
    )
    if result.ready:
        return success({"ready": True}, workspace=workspace, adapter=adapter, generations=generations)
    code = (
        ErrorCode.SCOPE_INCOMPATIBLE
        if result.code is ReadinessCode.SCOPE_INCOMPATIBLE
        else ErrorCode.NOT_READY
    )
    retry = None
    if result.retry is not None:
        retry = RetryMetadata(
            retryable=result.retry.retryable,
            retry_after_seconds=result.retry.retry_after_seconds,
            waited_seconds=result.retry.waited_seconds,
            timeout_seconds=result.retry.timeout_seconds,
            target_generation=result.retry.target_generation,
            observed_generation=result.retry.observed_generation,
        )
    details: dict[str, JsonValue] = {"scope": result.scope.value}
    if result.path is not None:
        details["path"] = result.path
    if result.scope_error is not None:
        details.update(scope_error_details(result.scope_error))
        details["paths"] = tuple(result.scope_error.paths)
    return error(
        code,
        retry=retry,
        details=details,
        workspace=workspace,
        adapter=adapter,
        generations=generations,
    )


def from_executor_busy(exc: Exception, *, retry_after_seconds: float = 0.1) -> ErrorEnvelope:
    from serena_light.lsp.executor import ExecutorBusyError

    if not isinstance(exc, ExecutorBusyError):
        raise TypeError("expected ExecutorBusyError")
    return error(
        ErrorCode.BUSY,
        retry=RetryMetadata(retryable=True, retry_after_seconds=retry_after_seconds),
    )


def from_adapter_error(exc: Exception, *, adapter: AdapterMetadata | None = None) -> ErrorEnvelope:
    from serena_light.lsp.adapter import AdapterError, AdapterErrorCode

    if not isinstance(exc, AdapterError):
        raise TypeError("expected AdapterError")
    code = ErrorCode(AdapterErrorCode(exc.code).value)
    retry = None
    if exc.retry_after_seconds is not None:
        retry = RetryMetadata(
            retryable=code in {ErrorCode.NOT_READY, ErrorCode.COOLDOWN},
            retry_after_seconds=exc.retry_after_seconds,
        )
    return error(code, retry=retry, adapter=adapter)


def from_adapter_cooldown(exc: Exception, *, adapter: AdapterMetadata | None = None) -> ErrorEnvelope:
    """Convert the adapter's public cooldown failure specifically."""

    from serena_light.lsp.adapter import AdapterError, AdapterErrorCode

    if not isinstance(exc, AdapterError) or exc.code is not AdapterErrorCode.COOLDOWN:
        raise TypeError("expected AdapterError with COOLDOWN code")
    return from_adapter_error(exc, adapter=adapter)


def from_timeout(
    exc: BaseException,
    *,
    retryable: bool = True,
    timeout_seconds: float | None = None,
) -> ErrorEnvelope:
    """Convert a generic timeout while deliberately discarding its message."""

    if not isinstance(exc, TimeoutError):
        raise TypeError("expected TimeoutError")
    return error(
        ErrorCode.TIMED_OUT,
        retry=RetryMetadata(retryable=retryable, timeout_seconds=timeout_seconds),
    )


from_generic_timeout = from_timeout


# These schemas describe the serialized boundary and intentionally do not
# constrain tool-specific ``data`` or ``details`` beyond valid JSON.
SUCCESS_SCHEMA: Mapping[str, Any] = MappingProxyType(
    {
        "type": "object",
        "required": ["ok", "data"],
        "properties": {
            "ok": {"const": True},
            "data": {},
            "workspace": {"type": "object"},
            "adapter": {"type": "object"},
            "generations": {"type": "object"},
            "truncation": {"type": "object"},
        },
        "additionalProperties": False,
    }
)
ERROR_SCHEMA: Mapping[str, Any] = MappingProxyType(
    {
        "type": "object",
        "required": ["ok", "error"],
        "properties": {
            "ok": {"const": False},
            "error": {
                "type": "object",
                "required": ["code", "message", "retry", "details"],
                "properties": {
                    "code": {"enum": [code.value for code in ErrorCode]},
                    "message": {"type": "string"},
                    "retry": {"type": ["object", "null"]},
                    "details": {"type": "object"},
                },
                "additionalProperties": False,
            },
            "workspace": {"type": "object"},
            "adapter": {"type": "object"},
            "generations": {"type": "object"},
        },
        "additionalProperties": False,
    }
)
ENVELOPE_SCHEMA: Mapping[str, Any] = MappingProxyType({"oneOf": [SUCCESS_SCHEMA, ERROR_SCHEMA]})
