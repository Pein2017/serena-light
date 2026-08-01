"""Adapt one successful rich internal diagnostics envelope to the compact schema.

This module is the single public presentation boundary for diagnostics
success: it flattens the verbose group/engine internals from
:mod:`serena_light.tools.diagnostics` into the fixed compact schema.  Callers
must invoke it only for an already-successful diagnostics envelope; every
operational diagnostics failure (``NOT_READY``, ``TIMED_OUT``,
``INVALID_INPUT``, ``SYMBOL_NOT_FOUND``, ``AMBIGUOUS_SYMBOL``, ...) stays on
the rich envelope path untouched.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from mcp import types

from serena_light.tools.compact import (
    DEFAULT_MAX_ANSWER_CHARS,
    canonical_json,
    compact_range,
    minimum_required_chars_result,
    render_payload,
    validate_max_answer_chars,
)
from serena_light.tools.envelopes import ErrorCode, error

_ADVISORY_AUTHORITY = "advisory"


def compact_diagnostics_result(
    envelope: Mapping[str, object],
    *,
    max_answer_chars: int = DEFAULT_MAX_ANSWER_CHARS,
) -> types.CallToolResult:
    """Convert one successful rich internal diagnostics envelope without leaks."""

    try:
        budget = validate_max_answer_chars(max_answer_chars)
    except (TypeError, ValueError):
        return _invalid_input("max_answer_chars")

    try:
        if envelope.get("ok") is not True:
            raise ValueError("compact diagnostics adapter requires diagnostics success")
        data = _mapping(envelope.get("data"), "data")
        workspace = _string(_mapping(envelope.get("workspace"), "workspace").get("root"), "workspace.root")
        path = _string(data.get("relative_path"), "relative_path")
        engine = _mapping(data.get("engine"), "engine")
        advisory = engine.get("authority") == _ADVISORY_AUTHORITY
        findings = _flatten_findings(data)
        omitted = _internal_omitted(envelope)
    except (KeyError, TypeError, ValueError):
        return _malformed_result()

    retained = findings
    while True:
        if findings and not retained:
            minimum = _payload(
                workspace,
                path,
                findings[:1],
                advisory,
                omitted + len(findings) - 1,
            )
            return minimum_required_chars_result(len(canonical_json(minimum)))
        payload = _payload(workspace, path, retained, advisory, omitted + (len(findings) - len(retained)))
        text = canonical_json(payload)
        if len(text) <= budget:
            return render_payload(payload)
        if not retained:
            return minimum_required_chars_result(len(text))
        retained = retained[:-1]


def _payload(
    workspace: str,
    path: str,
    findings: Sequence[Mapping[str, Any]],
    advisory: bool,
    omitted: int,
) -> dict[str, Any]:
    file: dict[str, Any] = {"path": path, "diagnostics": [dict(finding) for finding in findings]}
    if advisory:
        file["authority"] = _ADVISORY_AUTHORITY
    return {"ok": True, "data": {"workspace": workspace, "files": [file], "omitted": omitted}}


def _flatten_findings(data: Mapping[str, object]) -> tuple[dict[str, Any], ...]:
    findings: list[dict[str, Any]] = []
    for raw_group in _sequence(data.get("groups"), "groups"):
        group = _mapping(raw_group, "group")
        name_path = _string(group.get("name_path"), "group.name_path")
        symbol = None if name_path == "<file>" else name_path
        for raw_finding in _sequence(group.get("findings"), "group.findings"):
            findings.append(_flatten_finding(_mapping(raw_finding, "finding"), symbol))
    return tuple(findings)


def _flatten_finding(finding: Mapping[str, object], symbol: str | None) -> dict[str, Any]:
    start, end = compact_range(_mapping(finding.get("range"), "finding.range"))
    value: dict[str, Any] = {
        "severity": _string(finding.get("severity"), "finding.severity"),
        "range": [[start[0], start[1]], [end[0], end[1]]],
        "message": _string(finding.get("message"), "finding.message"),
    }
    if symbol is not None:
        value["symbol"] = symbol
    source = finding.get("source")
    if source is not None:
        value["source"] = _string(source, "finding.source")
    code = finding.get("code")
    if code is not None:
        if isinstance(code, bool) or not isinstance(code, str | int):
            raise TypeError("finding.code must be a string or integer")
        value["code"] = code
    return value


def _internal_omitted(envelope: Mapping[str, object]) -> int:
    truncation = envelope.get("truncation")
    if truncation is None:
        return 0
    value = _mapping(truncation, "truncation").get("omitted_count", 0)
    omitted = _integer(value, "truncation.omitted_count")
    if omitted < 0:
        raise ValueError("truncation.omitted_count must be non-negative")
    return omitted


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a mapping")
    return cast(Mapping[str, Any], value)


def _sequence(value: object, field: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        raise TypeError(f"{field} must be a sequence")
    return cast(Sequence[object], value)


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError(f"{field} must be a non-empty string")
    return value


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an integer")
    return value


def _invalid_input(field: str) -> types.CallToolResult:
    envelope = error(ErrorCode.INVALID_INPUT, details={"field": field})
    return render_payload(envelope.to_dict())


def _malformed_result() -> types.CallToolResult:
    envelope = error(
        ErrorCode.UNSUPPORTED,
        details={"reason": "malformed_diagnostics_success"},
    )
    return render_payload(envelope.to_dict())


__all__ = ("compact_diagnostics_result",)
