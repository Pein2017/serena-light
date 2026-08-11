"""Compact public projection of the daemon's rich lease-local runtime status."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

_LANGUAGE_ORDER = ("python", "typescript")
_HEALTHY_PHASE_STATES = {
    "cold": "cold",
    "starting": "warming",
    "warming": "warming",
    "global_warming": "warming",
    "document_ready": "ready",
    "ready": "ready",
}
_MAX_SCOPE_ITEMS = 50


def compact_runtime_status(
    raw: Mapping[str, object],
    *,
    build_identity: str,
    server_version: str,
    protocol_version: str,
) -> dict[str, object]:
    """Return the fixed Agent-facing status DTO without copying debug history."""

    build = {
        "identity": build_identity,
        "server_version": server_version,
        "protocol_version": protocol_version,
    }
    binding = _mapping(raw.get("binding"))
    runtime = _mapping(raw.get("runtime"))
    if not binding or not runtime:
        return {
            "workspace": None,
            "build": build,
            "languages": [],
            "executor": {"active": 0, "queued": 0, "capacity": 0},
            "issues": [
                {
                    "code": "WORKSPACE_UNBOUND",
                    "retryable": False,
                    "remediation": "activate_workspace",
                }
            ],
        }

    identity = _mapping(runtime.get("identity"))
    workspace = {
        "root": _text(identity.get("root"), fallback=binding.get("identity")),
        "working_subdirectory": _text(binding.get("working_subdirectory")),
        "kind": _text(identity.get("kind"), fallback="unknown"),
        "python_environment": _text(
            identity.get("python_environment"),
            fallback=binding.get("python_environment") or "ms",
        ),
    }
    adapters = _mapping(runtime.get("adapters"))
    unavailable = _mapping(runtime.get("unavailable_language_families"))
    languages: list[dict[str, object]] = []
    issues: list[dict[str, object]] = []
    for language in _LANGUAGE_ORDER:
        adapter = _mapping(adapters.get(language))
        unavailable_family = _mapping(unavailable.get(language))
        if adapter:
            state, issue = _adapter_public_state(language, adapter)
        elif unavailable_family:
            state, issue = _unavailable_public_state(language, unavailable_family)
        else:
            continue
        languages.append({"language": language, "state": state})
        if issue is not None:
            issues.append(issue)

    executor = _executor_summary(_mapping(runtime.get("executor")))
    executor_issue = _executor_issue(_mapping(runtime.get("executor")), executor)
    if executor_issue is not None:
        issues.append(executor_issue)
    return {
        "workspace": workspace,
        "build": build,
        "languages": languages,
        "executor": executor,
        "issues": issues,
    }


def _adapter_public_state(
    language: str,
    adapter: Mapping[str, object],
) -> tuple[str, dict[str, object] | None]:
    phase = _text(adapter.get("phase"), fallback="unknown")
    healthy = _HEALTHY_PHASE_STATES.get(phase)
    if healthy is not None:
        return healthy, None
    if phase == "cooldown":
        cooldown = _mapping(adapter.get("cooldown"))
        crash = _mapping(adapter.get("crash"))
        issue: dict[str, object] = {
            "code": "ADAPTER_COOLDOWN",
            "language": language,
            "retryable": True,
            "remediation": "retry_after_cooldown",
            "phase": phase,
        }
        remaining = cooldown.get("remaining_seconds")
        if isinstance(remaining, int | float) and not isinstance(remaining, bool):
            issue["retry_after_seconds"] = max(0.0, float(remaining))
        failure = crash.get("last_error")
        if isinstance(failure, str) and failure:
            issue["failure"] = failure
        return "cooldown", issue
    code = "ADAPTER_FAILED" if phase == "degraded" else "ADAPTER_UNAVAILABLE"
    return (
        "failed" if phase == "degraded" else "unavailable",
        {
            "code": code,
            "language": language,
            "retryable": phase != "stopping",
            "remediation": "retry_semantic_call" if phase != "stopping" else "reactivate_workspace",
            "phase": phase,
        },
    )


def _unavailable_public_state(
    language: str,
    unavailable: Mapping[str, object],
) -> tuple[str, dict[str, object]]:
    failure = _mapping(unavailable.get("error"))
    code = _text(failure.get("code"), fallback="LANGUAGE_UNAVAILABLE")
    state = {
        "NOT_READY": "warming",
        "COOLDOWN": "cooldown",
        "SCOPE_INCOMPATIBLE": "unavailable",
        "STOPPED": "unavailable",
    }.get(code, "failed")
    retryable = code in {"NOT_READY", "COOLDOWN", "TIMED_OUT"}
    remediation = {
        "SCOPE_INCOMPATIBLE": "correct_or_bypass_language_scope",
        "COOLDOWN": "retry_after_cooldown",
        "NOT_READY": "retry_semantic_call",
        "TIMED_OUT": "retry_semantic_call",
        "STOPPED": "reactivate_workspace",
    }.get(code, "inspect_language_configuration")
    issue: dict[str, object] = {
        "code": code,
        "language": language,
        "retryable": retryable,
        "remediation": remediation,
    }
    for field in ("selected_native_config", "project_kind", "phase"):
        value = unavailable.get(field)
        if isinstance(value, str) and value:
            issue[field] = value
    paths = failure.get("paths")
    if isinstance(paths, Sequence) and not isinstance(paths, str | bytes):
        issue["paths"] = [value for value in paths[:_MAX_SCOPE_ITEMS] if isinstance(value, str)]
    retry_after = failure.get("retry_after_seconds", unavailable.get("retry_after_seconds"))
    if isinstance(retry_after, int | float) and not isinstance(retry_after, bool):
        issue["retry_after_seconds"] = max(0.0, float(retry_after))
    current_failure = failure.get("message", unavailable.get("failure"))
    if isinstance(current_failure, str) and current_failure:
        issue["failure"] = current_failure
    outside = _bounded_difference(unavailable.get("configured_program_outside_trust"))
    if outside is not None and outside["total"]:
        issue["configured_program_outside_trust"] = outside
    return state, issue


def _bounded_difference(value: object) -> dict[str, object] | None:
    raw = _mapping(value)
    if not raw:
        return None
    raw_items = raw.get("items")
    items: list[dict[str, str]] = []
    if isinstance(raw_items, Sequence) and not isinstance(raw_items, str | bytes):
        for item in raw_items[:_MAX_SCOPE_ITEMS]:
            candidate = _mapping(item)
            path = candidate.get("path")
            reason = candidate.get("reason")
            if isinstance(path, str) and isinstance(reason, str):
                items.append({"path": path, "reason": reason})
    total = raw.get("total")
    total_value = total if isinstance(total, int) and not isinstance(total, bool) and total >= 0 else len(items)
    digest = raw.get("digest")
    omitted = raw.get("omitted_count")
    return {
        "items": tuple(items),
        "total": total_value,
        "digest": digest if isinstance(digest, str) else "",
        "omitted_count": (
            omitted
            if isinstance(omitted, int) and not isinstance(omitted, bool) and omitted >= 0
            else max(0, total_value - len(items))
        ),
    }


def _executor_summary(raw: Mapping[str, object]) -> dict[str, int]:
    queued = _non_negative_int(raw.get("queue_size"))
    capacity = _non_negative_int(raw.get("queue_capacity"))
    active_value = raw.get("active")
    active = int(active_value) if isinstance(active_value, bool) else _non_negative_int(active_value)
    return {"active": active, "queued": queued, "capacity": capacity}


def _executor_issue(
    raw: Mapping[str, object],
    summary: Mapping[str, int],
) -> dict[str, object] | None:
    if raw.get("stopping") is True:
        return {
            "code": "EXECUTOR_STOPPING",
            "retryable": False,
            "remediation": "reactivate_workspace",
        }
    capacity = summary["capacity"]
    queued = summary["queued"]
    if capacity > 0 and queued >= capacity:
        return {
            "code": "EXECUTOR_SATURATED",
            "retryable": True,
            "remediation": "retry_after_queue_drains",
            "queued": queued,
            "capacity": capacity,
        }
    return None


def _mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}


def _text(value: object, *, fallback: object = "") -> str:
    if isinstance(value, str):
        return value
    return fallback if isinstance(fallback, str) else str(fallback)


def _non_negative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
