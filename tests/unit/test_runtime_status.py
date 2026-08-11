from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

import pytest

from serena_light.tools.runtime_status import compact_runtime_status

_BUILD = "b" * 64


def _raw_status(
    *,
    adapters: Mapping[str, object] | None = None,
    unavailable: Mapping[str, object] | None = None,
    executor: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "lease": {"lease_id": "private-lease", "issued_at": 12.0},
        "binding": {
            "identity": "/workspace",
            "working_subdirectory": "/workspace/subdir",
            "python_environment": "ms",
            "python_interpreter": "/private/ms/bin/python",
        },
        "runtime": {
            "identity": {
                "root": "/workspace",
                "kind": "git",
                "python_environment": "ms",
                "python_interpreter": "/private/ms/bin/python",
            },
            "trust_inventory": {"count": 4, "sha256": "a" * 64},
            "adapters": dict(adapters or {}),
            "unavailable_language_families": dict(unavailable or {}),
            "skipped_language_families": (),
            "executor": dict(executor or {"queue_size": 0, "queue_capacity": 32, "active": False}),
        },
    }


def _present(raw: Mapping[str, object]) -> dict[str, object]:
    return compact_runtime_status(
        raw,
        build_identity=_BUILD,
        server_version="0.1.0",
        protocol_version="2025-11-25",
    )


@pytest.mark.parametrize(
    ("phase", "state"),
    [
        ("cold", "cold"),
        ("starting", "warming"),
        ("global_warming", "warming"),
        ("document_ready", "ready"),
        ("ready", "ready"),
    ],
)
def test_healthy_status_uses_fixed_compact_shape(phase: str, state: str) -> None:
    result = _present(
        _raw_status(
            adapters={
                "python": {
                    "phase": phase,
                    "engine": {"executable": "/private/pyright", "interpreter": "/private/python"},
                    "generations": {"trust": 3, "program": 5},
                    "transitions": [{"phase": phase, "timestamp": 7.0}],
                },
                "typescript": {
                    "phase": "ready",
                    "raw_providers": {"implementation": True},
                    "crash": {"total": 0},
                },
            }
        )
    )

    assert result == {
        "workspace": {
            "root": "/workspace",
            "working_subdirectory": "/workspace/subdir",
            "kind": "git",
            "python_environment": "ms",
        },
        "build": {
            "identity": _BUILD,
            "server_version": "0.1.0",
            "protocol_version": "2025-11-25",
        },
        "languages": [
            {"language": "python", "state": state},
            {"language": "typescript", "state": "ready"},
        ],
        "executor": {"active": 0, "queued": 0, "capacity": 32},
        "issues": [],
    }
    serialized = json.dumps(result)
    for forbidden in (
        "lease_id",
        "issued_at",
        "daemon_id",
        "python_interpreter",
        "executable",
        "sha256",
        "generations",
        "transitions",
        "raw_providers",
        "crash",
    ):
        assert forbidden not in serialized


def test_scope_incompatible_family_keeps_only_bounded_actionable_evidence() -> None:
    difference = {
        "items": ({"path": "ignored.py", "reason": "git_ignored"},),
        "total": 1,
        "digest": "d" * 64,
        "omitted_count": 0,
    }
    result = _present(
        _raw_status(
            adapters={"typescript": {"phase": "ready", "transitions": [{"reason": "private"}]}},
            unavailable={
                "python": {
                    "error": {"code": "SCOPE_INCOMPATIBLE", "paths": ("ignored.py",)},
                    "scope_compatible": False,
                    "selected_native_config": "pyrightconfig.json",
                    "project_kind": "native_config",
                    "trust_inventory": {"count": 20, "sha256": "a" * 64},
                    "configured_program": {"count": 21, "sha256": "b" * 64},
                    "configured_program_outside_trust": difference,
                }
            },
        )
    )

    assert result["languages"] == [
        {"language": "python", "state": "unavailable"},
        {"language": "typescript", "state": "ready"},
    ]
    assert result["issues"] == [
        {
            "code": "SCOPE_INCOMPATIBLE",
            "language": "python",
            "retryable": False,
            "remediation": "correct_or_bypass_language_scope",
            "selected_native_config": "pyrightconfig.json",
            "project_kind": "native_config",
            "paths": ["ignored.py"],
            "configured_program_outside_trust": difference,
        }
    ]
    assert "transitions" not in json.dumps(result)
    assert "trust_inventory" not in json.dumps(result)
    assert "configured_program\"" not in json.dumps(result)


def test_cooldown_keeps_current_reason_and_retry_timing_without_history() -> None:
    result = _present(
        _raw_status(
            adapters={
                "python": {
                    "phase": "cooldown",
                    "cooldown": {"until": 50.0, "remaining_seconds": 4.5},
                    "crash": {"total": 9, "window_count": 3, "last_error": "server exited"},
                    "transitions": [{"reason": "older failure"}],
                }
            }
        )
    )

    assert result["languages"] == [{"language": "python", "state": "cooldown"}]
    assert result["issues"] == [
        {
            "code": "ADAPTER_COOLDOWN",
            "language": "python",
            "retryable": True,
            "remediation": "retry_after_cooldown",
            "phase": "cooldown",
            "retry_after_seconds": 4.5,
            "failure": "server exited",
        }
    ]
    assert "older failure" not in json.dumps(result)
    assert "window_count" not in json.dumps(result)


def test_executor_activity_is_compact_and_saturation_is_one_issue() -> None:
    active = _present(
        _raw_status(
            adapters={"python": {"phase": "ready"}},
            executor={"queue_size": 2, "queue_capacity": 4, "active": True, "stopping": False},
        )
    )
    assert active["executor"] == {"active": 1, "queued": 2, "capacity": 4}
    assert active["issues"] == []

    saturated = _present(
        _raw_status(
            adapters={"python": {"phase": "ready"}},
            executor={"queue_size": 4, "queue_capacity": 4, "active": True, "stopping": False},
        )
    )
    assert saturated["issues"] == [
        {
            "code": "EXECUTOR_SATURATED",
            "retryable": True,
            "remediation": "retry_after_queue_drains",
            "queued": 4,
            "capacity": 4,
        }
    ]


def test_unbound_status_has_no_lease_or_runtime_identity() -> None:
    result = _present({"lease": {"lease_id": "private"}, "binding": None, "runtime": None})

    assert result == {
        "workspace": None,
        "build": {
            "identity": _BUILD,
            "server_version": "0.1.0",
            "protocol_version": "2025-11-25",
        },
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
    assert "private" not in json.dumps(result)


def test_shared_runtime_status_keeps_each_lease_working_subdirectory() -> None:
    first_raw = _raw_status(adapters={"python": {"phase": "cold"}})
    second_raw = _raw_status(adapters={"python": {"phase": "cold"}})
    first_binding = cast(dict[str, object], first_raw["binding"])
    second_binding = cast(dict[str, object], second_raw["binding"])
    first_binding["working_subdirectory"] = "/workspace/first"
    second_binding["working_subdirectory"] = "/workspace/second"

    first = _present(first_raw)
    second = _present(second_raw)

    assert first["workspace"] == {
        "root": "/workspace",
        "working_subdirectory": "/workspace/first",
        "kind": "git",
        "python_environment": "ms",
    }
    assert second["workspace"] == {
        "root": "/workspace",
        "working_subdirectory": "/workspace/second",
        "kind": "git",
        "python_environment": "ms",
    }
