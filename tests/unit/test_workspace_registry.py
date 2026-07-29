from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from serena_light.workspace.registry import ResolvedWorkspace, WorkspaceRuntimeRegistry


def resolved(identity: str, subdirectory: str) -> ResolvedWorkspace[str]:
    return ResolvedWorkspace(identity=identity, working_subdirectory=Path(subdirectory))


def test_same_root_sessions_share_one_runtime_and_distinct_leases() -> None:
    created: list[object] = []

    def factory(identity: str) -> object:
        assert identity == "/data/project"
        runtime = object()
        created.append(runtime)
        return runtime

    registry = WorkspaceRuntimeRegistry[str, object, str](factory)
    first = registry.activate("session-a", resolved("/data/project", "."))
    second = registry.activate("session-b", resolved("/data/project", "src"))

    assert first.runtime is second.runtime is created[0]
    assert first.lease.id != second.lease.id
    assert registry.runtime_state("/data/project").reference_count == 2  # type: ignore[union-attr]


def test_distinct_roots_are_isolated() -> None:
    registry = WorkspaceRuntimeRegistry[str, object, str](lambda _identity: object())
    first = registry.activate("session-a", resolved("/data/one", "."))
    second = registry.activate("session-b", resolved("/data/two", "."))

    assert first.runtime is not second.runtime
    assert registry.runtime_state("/data/one").reference_count == 1  # type: ignore[union-attr]
    assert registry.runtime_state("/data/two").reference_count == 1  # type: ignore[union-attr]


def test_same_root_activation_reuses_lease_and_updates_subdirectory() -> None:
    registry = WorkspaceRuntimeRegistry[str, object, str](lambda _identity: object())
    first = registry.activate("session", resolved("/data/project", "one"))
    second = registry.activate("session", resolved("/data/project", "two/nested"))

    assert second.lease is first.lease
    assert second.working_subdirectory == Path("two/nested")
    assert registry.runtime_state("/data/project").reference_count == 1  # type: ignore[union-attr]


def test_failed_cross_root_resolution_or_acquisition_preserves_old_binding() -> None:
    def factory(identity: str) -> object:
        if identity == "/data/broken":
            raise RuntimeError("runtime unavailable")
        return object()

    registry = WorkspaceRuntimeRegistry[str, object, str](factory)
    old = registry.activate("session", resolved("/data/old", "."))

    with pytest.raises(RuntimeError, match="identity invalid"):
        registry.activate_path(
            "session", Path("/data/bad"), lambda _path: (_ for _ in ()).throw(RuntimeError("identity invalid"))
        )
    with pytest.raises(RuntimeError, match="runtime unavailable"):
        registry.activate("session", resolved("/data/broken", "."))

    assert registry.binding_for("session") == old
    assert registry.runtime_state("/data/old").reference_count == 1  # type: ignore[union-attr]
    assert registry.runtime_state("/data/broken") is None


def test_cross_root_swap_acquires_then_releases_old_lease() -> None:
    registry = WorkspaceRuntimeRegistry[str, object, str](lambda _identity: object())
    old = registry.activate("session", resolved("/data/old", "."))
    new = registry.activate("session", resolved("/data/new", "src"))

    assert registry.binding_for("session") == new
    assert old.lease.id != new.lease.id
    assert registry.runtime_state("/data/old").reference_count == 0  # type: ignore[union-attr]
    assert registry.runtime_state("/data/old").idle_eligible  # type: ignore[union-attr]
    assert registry.runtime_state("/data/new").reference_count == 1  # type: ignore[union-attr]


def test_release_is_idempotent_and_lease_release_detaches_binding() -> None:
    registry = WorkspaceRuntimeRegistry[str, object, str](lambda _identity: object())
    binding = registry.activate("session", resolved("/data/project", "."))

    assert registry.release_lease(binding.lease.id)
    assert registry.binding_for("session") is None
    assert not registry.release_lease(binding.lease.id)
    assert not registry.release("session")
    state = registry.runtime_state("/data/project")
    assert state is not None
    assert state.reference_count == 0
    assert state.idle_eligible


def test_concurrent_sessions_keep_reference_count_consistent() -> None:
    registry = WorkspaceRuntimeRegistry[str, object, int](lambda _identity: object())
    sessions = list(range(64))

    with ThreadPoolExecutor(max_workers=16) as executor:
        list(
            executor.map(lambda session: registry.activate(session, resolved("/data/project", str(session))), sessions)
        )

    state = registry.runtime_state("/data/project")
    assert state is not None
    assert state.reference_count == len(sessions)

    with ThreadPoolExecutor(max_workers=16) as executor:
        assert all(executor.map(registry.release, sessions))

    state = registry.runtime_state("/data/project")
    assert state is not None
    assert state.reference_count == 0
    assert state.idle_eligible


def test_retire_idle_uses_compare_and_remove_and_allows_fresh_runtime() -> None:
    created: list[object] = []

    def factory(_identity: str) -> object:
        runtime = object()
        created.append(runtime)
        return runtime

    registry = WorkspaceRuntimeRegistry[str, object, str](factory)
    binding = registry.activate("session", resolved("/data/project", "."))

    assert registry.retire_idle("/data/project", binding.runtime) is None
    assert registry.release("session")
    assert registry.retire_idle("/data/project", object()) is None
    assert registry.retire_idle("/data/project", binding.runtime) is binding.runtime
    assert registry.runtime_state("/data/project") is None

    rebound = registry.activate("new-session", resolved("/data/project", "."))
    assert rebound.runtime is created[1]
    assert rebound.runtime is not binding.runtime
