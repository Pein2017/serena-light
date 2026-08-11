"""Final client-visible presentation for typed tool failures."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, cast

from mcp import types

from serena_light.tools.compact import (
    canonical_json,
    compact_range,
    render_payload,
    symbol_kind,
    validate_max_answer_chars,
)
from serena_light.tools.envelopes import ErrorCode

_DETERMINISTIC_COMPACT_ERRORS = frozenset(
    {
        ErrorCode.INVALID_INPUT.value,
        ErrorCode.INVALID_PATH.value,
        ErrorCode.SYMBOL_NOT_FOUND.value,
    }
)
_RUNTIME_AUTHORITY_DETAIL_FIELDS = frozenset(
    {"adapter", "configured_program", "engine", "generations", "interpreter"}
)
_CORRECTION_ECHO_REMOVAL_ORDER = (
    "name_path",
    "regex",
    "path",
    "paths",
    "relative_path",
    "method",
)


class RecoveryAction(StrEnum):
    """Closed machine-readable actions for deterministic query recovery."""

    GET_SYMBOLS_OVERVIEW = "get_symbols_overview"
    ACTIVATE_WORKSPACE_IF_OTHER_ROOT = "activate_workspace_if_other_root"
    REACTIVATE_WITH_PATH_ENVIRONMENT = "reactivate_with_path_environment"


def render_error_result(
    envelope: Mapping[str, object],
    *,
    max_answer_chars: int | None = None,
) -> types.CallToolResult:
    """Render one typed failure, compacting only deterministic correction errors."""

    if envelope.get("ok") is not False:
        raise ValueError("error presentation requires ok=false")
    raw_error_value = envelope.get("error")
    if not isinstance(raw_error_value, Mapping):
        raise ValueError("error presentation requires an error object")
    raw_error = cast(Mapping[Any, Any], raw_error_value)
    code = raw_error.get("code")
    message = raw_error.get("message")
    if not isinstance(code, str) or not isinstance(message, str):
        raise ValueError("error presentation requires string code and message")

    if code == ErrorCode.AMBIGUOUS_SYMBOL.value:
        payload = _compact_ambiguity(envelope, raw_error, code, message)
        return render_payload(
            _bound_ambiguity(payload, validate_max_answer_chars(max_answer_chars))
            if max_answer_chars is not None
            else payload
        )

    if code not in _DETERMINISTIC_COMPACT_ERRORS:
        return render_payload(_mapping_copy(envelope))

    compact_error: dict[str, Any] = {"code": code, "message": message}
    details = raw_error.get("details")
    if isinstance(details, Mapping) and details:
        compact_details = {
            str(key): value
            for key, value in cast(Mapping[Any, Any], details).items()
            if isinstance(key, str) and key not in _RUNTIME_AUTHORITY_DETAIL_FIELDS
        }
        _validate_recovery_action(compact_details)
        if compact_details:
            compact_error["details"] = compact_details
    payload: dict[str, Any] = {"ok": False, "error": compact_error}
    workspace = envelope.get("workspace")
    if isinstance(workspace, Mapping):
        root = cast(Mapping[Any, Any], workspace).get("root")
        if isinstance(root, str) and root:
            payload["workspace"] = root
    return render_payload(
        _bound_deterministic(payload, validate_max_answer_chars(max_answer_chars))
        if max_answer_chars is not None
        else payload
    )


def _compact_ambiguity(
    envelope: Mapping[str, object],
    raw_error: Mapping[Any, Any],
    code: str,
    message: str,
) -> dict[str, Any]:
    """Keep correction evidence for ambiguity without runtime-authority repetition."""

    details_value = raw_error.get("details")
    if not isinstance(details_value, Mapping):
        return _mapping_copy(envelope)
    details = cast(Mapping[Any, Any], details_value)
    compact_details: dict[str, Any] = {}
    for field in ("relative_path", "name_path", "regex", "occurrence_count"):
        if field in details:
            compact_details[field] = details[field]
    candidates = details.get("candidates")
    if isinstance(candidates, list | tuple):
        compact_details["candidates"] = [_compact_ambiguity_candidate(candidate) for candidate in candidates]
    existing_omitted = details.get("omitted_count", 0)
    if isinstance(existing_omitted, bool) or not isinstance(existing_omitted, int) or existing_omitted < 0:
        raise ValueError("ambiguous candidate omitted_count must be non-negative")
    if details.get("truncated") is True or existing_omitted:
        compact_details["truncated"] = True
        compact_details["omitted_count"] = existing_omitted
    _validate_recovery_action(compact_details)

    payload: dict[str, Any] = {
        "ok": False,
        "error": {"code": code, "message": message, "details": compact_details},
    }
    workspace = envelope.get("workspace")
    if isinstance(workspace, Mapping):
        root = cast(Mapping[Any, Any], workspace).get("root")
        if isinstance(root, str) and root:
            payload["workspace"] = root
    return payload


def _compact_ambiguity_candidate(candidate: object) -> object:
    """Keep only fields that identify and disambiguate one symbol candidate."""

    if not isinstance(candidate, Mapping):
        return candidate
    source = cast(Mapping[Any, Any], candidate)
    compact: dict[str, Any] = {}
    for field in ("relative_path", "name_path"):
        value = source.get(field)
        if isinstance(value, str) and value:
            compact[field] = value
    if "name_path" not in compact:
        name = source.get("name")
        if isinstance(name, str) and name:
            compact["name"] = name
    kind = source.get("kind")
    if isinstance(kind, int) and not isinstance(kind, bool):
        compact["kind"] = symbol_kind(kind)
    elif isinstance(kind, str) and kind:
        compact["kind"] = kind
    candidate_range = source.get("range")
    if isinstance(candidate_range, Mapping):
        try:
            compact["range"] = compact_range(cast(Mapping[str, object], candidate_range))
        except (TypeError, ValueError):
            # A malformed internal candidate is still useful correction evidence;
            # preserve it rather than converting a typed ambiguity into transport failure.
            return _mapping_copy(source)
    return compact or _mapping_copy(source)


def _bound_ambiguity(payload: dict[str, Any], budget: int) -> dict[str, Any]:
    """Remove only trailing candidates and then whole optional context fields."""

    if len(canonical_json(payload)) <= budget:
        return payload
    error_value = cast(dict[str, Any], payload["error"])
    details = cast(dict[str, Any], error_value["details"])
    candidates_value = details.get("candidates")
    if isinstance(candidates_value, list):
        candidates = candidates_value
        original = len(candidates) + cast(int, details.get("omitted_count", 0))
        while candidates:
            candidates.pop()
            details["truncated"] = True
            details["omitted_count"] = original - len(candidates)
            if len(canonical_json(payload)) <= budget:
                return payload

    # Query echoes and workspace are useful but optional once the typed error
    # truthfully says every candidate was omitted. Remove them whole, never slice.
    for field in ("regex", "name_path", "relative_path", "occurrence_count"):
        details.pop(field, None)
        if len(canonical_json(payload)) <= budget:
            return payload
    payload.pop("workspace", None)
    if len(canonical_json(payload)) <= budget:
        return payload
    raise ValueError("minimum ambiguous error exceeds the public answer budget")


def _bound_deterministic(payload: dict[str, Any], budget: int) -> dict[str, Any]:
    """Remove whole optional correction fields until a deterministic error fits."""

    if len(canonical_json(payload)) <= budget:
        return payload
    error_value = cast(dict[str, Any], payload["error"])
    details_value = error_value.get("details")
    details = details_value if isinstance(details_value, dict) else None
    if details is not None:
        for field in _CORRECTION_ECHO_REMOVAL_ORDER:
            details.pop(field, None)
            if len(canonical_json(payload)) <= budget:
                return payload

    if details is not None:
        remaining = sorted(
            (field for field in details if field != "next_action"),
            key=lambda field: (len(canonical_json(details[field])), field),
            reverse=True,
        )
        for field in remaining:
            details.pop(field)
            if not details:
                error_value.pop("details", None)
            if len(canonical_json(payload)) <= budget:
                return payload

    # The active root and the closed recovery action are the decision-bearing
    # facts.  Keep them ahead of long query echoes and incidental details.
    payload.pop("workspace", None)
    if len(canonical_json(payload)) <= budget:
        return payload
    raise ValueError("minimum deterministic error exceeds the public answer budget")


def _validate_recovery_action(details: Mapping[str, Any]) -> None:
    """Reject free-form recovery advice at the client-visible boundary."""

    action = details.get("next_action")
    if action is None:
        return
    if not isinstance(action, str):
        raise ValueError("recovery action must be a string")
    try:
        RecoveryAction(action)
    except ValueError as exc:
        raise ValueError("recovery action is not recognized") from exc


def _mapping_copy(value: Mapping[Any, Any]) -> dict[str, Any]:
    """Copy JSON-shaped mappings without retaining mutable caller ownership."""

    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError("client-visible JSON keys must be strings")
        if isinstance(item, Mapping):
            result[key] = _mapping_copy(cast(Mapping[Any, Any], item))
        elif isinstance(item, list | tuple):
            result[key] = [
                _mapping_copy(cast(Mapping[Any, Any], nested))
                if isinstance(nested, Mapping)
                else nested
                for nested in item
            ]
        else:
            result[key] = item
    return result


__all__ = ["RecoveryAction", "render_error_result"]
