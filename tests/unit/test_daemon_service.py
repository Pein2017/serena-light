from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from serena_light import cli
from serena_light.daemon.leases import LEASE_EXPIRY_SECONDS, WARM_GRACE_SECONDS, LeaseLifecycle
from serena_light.daemon.server import LeaseExpiredError
from serena_light.daemon.service import WorkspaceDaemonService
from serena_light.workspace.registry import ResolvedWorkspace, WorkspaceRuntimeRegistry


def run[ResultT](coroutine: Coroutine[Any, Any, ResultT]) -> ResultT:
    return asyncio.run(coroutine)


@dataclass(eq=False, slots=True)
class Runtime:
    identity: str
    freshness_count: int = 0
    freshness_error: Exception | None = None

    def ensure_fresh(self) -> None:
        self.freshness_count += 1
        if self.freshness_error is not None:
            raise self.freshness_error


@dataclass(slots=True)
class FakeClock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def resolution(path: Path) -> ResolvedWorkspace[str]:
    return ResolvedWorkspace(identity=str(path), working_subdirectory=path)


def make_service(
    *,
    clock: FakeClock | None = None,
    factory: Callable[[str], Runtime] | None = None,
    resolver: Callable[[Path], ResolvedWorkspace[str]] = resolution,
    debug_reporter: Callable[[str, str], object] | None = None,
) -> tuple[
    WorkspaceDaemonService[str, Runtime],
    WorkspaceRuntimeRegistry[str, Runtime, UUID],
    FakeClock,
    list[Runtime],
    list[int],
]:
    service_clock = FakeClock() if clock is None else clock
    runtime_factory = (lambda identity: Runtime(identity)) if factory is None else factory
    registry = WorkspaceRuntimeRegistry[str, Runtime, UUID](runtime_factory)
    stopped: list[Runtime] = []
    stop_threads: list[int] = []

    def stop(runtime: Runtime) -> None:
        stopped.append(runtime)
        stop_threads.append(threading.get_ident())

    service = WorkspaceDaemonService[str, Runtime](
        lifecycle=LeaseLifecycle[str, Runtime](clock=service_clock),
        registry=registry,
        resolver=resolver,
        runtime_stopper=stop,
        debug_reporter=debug_reporter,
    )
    return service, registry, service_clock, stopped, stop_threads


def test_debug_reporting_contains_only_bounded_lease_and_cleanup_summaries() -> None:
    events: list[tuple[str, str]] = []
    service, _registry, clock, _stopped, _threads = make_service(
        debug_reporter=lambda event, message: events.append((event, message))
    )

    async def scenario() -> None:
        lease_id = await acquire(service, "session-secret")
        await service.activate_workspace(lease_id=lease_id, absolute_path="/data/private-workspace")
        await service.release_lease(lease_id=lease_id, immediate=False)
        clock.advance(600)
        await service.sweep()

    run(scenario())

    assert events == [
        ("lease_grace", "reason=released holders=0 immediate=false"),
        ("workspace_cleanup", "reason=grace_expired holders=0 immediate=false"),
    ]
    assert "private-workspace" not in repr(events)
    assert "session-secret" not in repr(events)


async def acquire(service: WorkspaceDaemonService[str, Runtime], correlation: str = "mcp-session") -> str:
    grant = await service.acquire_lease(mcp_session_id=correlation)
    lease_id = grant["lease_id"]
    assert isinstance(lease_id, str)
    return lease_id


def test_acquire_returns_distinct_unbound_authority_independent_of_mcp_session() -> None:
    service, _registry, _clock, _stopped, _threads = make_service()

    async def scenario() -> None:
        first = await service.acquire_lease(mcp_session_id="same-correlation")
        second = await service.acquire_lease(mcp_session_id="same-correlation")

        assert first["bound"] is False
        assert second["bound"] is False
        assert first["lease_id"] != second["lease_id"]
        assert "mcp_session_id" not in first

    run(scenario())


def test_same_root_reuses_runtime_while_cross_root_bindings_remain_isolated() -> None:
    created: list[Runtime] = []

    def factory(identity: str) -> Runtime:
        runtime = Runtime(identity)
        created.append(runtime)
        return runtime

    service, _registry, _clock, _stopped, _threads = make_service(factory=factory)

    async def scenario() -> None:
        first = await acquire(service, "mcp-a")
        second = await acquire(service, "mcp-b")
        third = await acquire(service, "mcp-c")
        await service.activate_workspace(lease_id=first, absolute_path="/data/shared")
        await service.activate_workspace(lease_id=second, absolute_path="/data/shared")
        await service.activate_workspace(lease_id=third, absolute_path="/data/other")

        assert (await service.binding_for(lease_id=first)).runtime is (
            await service.binding_for(lease_id=second)
        ).runtime
        assert (await service.binding_for(lease_id=first)).runtime is not (
            await service.binding_for(lease_id=third)
        ).runtime
        assert len(created) == 2
        assert created[0].freshness_count == 2
        assert created[1].freshness_count == 1

    run(scenario())


def test_heartbeat_stays_responsive_while_runtime_acquisition_blocks() -> None:
    acquisition_started = threading.Event()
    unblock_acquisition = threading.Event()

    def factory(identity: str) -> Runtime:
        acquisition_started.set()
        assert unblock_acquisition.wait(timeout=5)
        return Runtime(identity)

    service, _registry, _clock, _stopped, _threads = make_service(factory=factory)

    async def scenario() -> None:
        lease_id = await acquire(service)
        activation = asyncio.create_task(service.activate_workspace(lease_id=lease_id, absolute_path="/data/blocked"))
        assert await asyncio.to_thread(acquisition_started.wait, 1)
        heartbeat = await asyncio.wait_for(service.heartbeat(lease_id=lease_id), timeout=0.2)
        assert heartbeat["lease_id"] == lease_id
        unblock_acquisition.set()
        await activation

    run(scenario())


def test_release_during_off_loop_resolution_prevents_registry_orphan() -> None:
    resolution_started = threading.Event()
    unblock_resolution = threading.Event()
    resolver_threads: list[int] = []

    def blocking_resolver(path: Path) -> ResolvedWorkspace[str]:
        resolver_threads.append(threading.get_ident())
        resolution_started.set()
        assert unblock_resolution.wait(timeout=5)
        return resolution(path)

    service, registry, _clock, stopped, _threads = make_service(resolver=blocking_resolver)
    caller_thread = threading.get_ident()

    async def scenario() -> None:
        lease_id = await acquire(service)
        activation = asyncio.create_task(service.activate_workspace(lease_id=lease_id, absolute_path="/data/new"))
        assert await asyncio.to_thread(resolution_started.wait, 1)
        released = await asyncio.wait_for(
            service.release_lease(lease_id=lease_id, immediate=False),
            timeout=0.2,
        )
        assert released["released"] is True
        unblock_resolution.set()

        with pytest.raises(LeaseExpiredError):
            await activation
        assert registry.runtime_state("/data/new") is None
        assert stopped == []

    run(scenario())
    assert resolver_threads and resolver_threads[0] != caller_thread


def test_expiry_during_cross_root_acquisition_rolls_back_orphan_runtime() -> None:
    acquisition_started = threading.Event()
    unblock_acquisition = threading.Event()

    def factory(identity: str) -> Runtime:
        if identity == "/data/new":
            acquisition_started.set()
            assert unblock_acquisition.wait(timeout=5)
        return Runtime(identity)

    service, registry, clock, stopped, _threads = make_service(factory=factory)

    async def scenario() -> None:
        lease_id = await acquire(service)
        await service.activate_workspace(lease_id=lease_id, absolute_path="/data/old")
        switch = asyncio.create_task(service.activate_workspace(lease_id=lease_id, absolute_path="/data/new"))
        assert await asyncio.to_thread(acquisition_started.wait, 1)
        clock.advance(LEASE_EXPIRY_SECONDS)
        unblock_acquisition.set()

        with pytest.raises(LeaseExpiredError):
            await switch
        assert registry.runtime_state("/data/new") is None
        assert [runtime.identity for runtime in stopped] == ["/data/new"]

    run(scenario())


def test_failed_same_root_refresh_preserves_prior_binding_and_holder_count() -> None:
    service, registry, _clock, stopped, _threads = make_service(
        resolver=lambda path: ResolvedWorkspace(identity=str(path.parent), working_subdirectory=path)
    )

    async def scenario() -> None:
        lease_id = await acquire(service)
        await service.activate_workspace(lease_id=lease_id, absolute_path="/data/project/old")
        prior = await service.binding_for(lease_id=lease_id)
        prior.runtime.freshness_error = TimeoutError("refresh stalled")

        failed = await service.activate_workspace(lease_id=lease_id, absolute_path="/data/project/new")
        assert failed["ok"] is False
        failure = cast(Mapping[str, object], failed["error"])
        assert failure.get("code") == "TIMED_OUT"

        restored = await service.binding_for(lease_id=lease_id)
        assert restored == prior
        state = registry.runtime_state("/data/project")
        assert state is not None and state.reference_count == 1
        assert service._lifecycle.active_holders("/data/project") == 1
        assert stopped == []

    run(scenario())


def test_failed_cross_root_refresh_restores_prior_binding_and_retires_new_runtime() -> None:
    created: list[Runtime] = []

    def factory(identity: str) -> Runtime:
        freshness_error = TimeoutError("refresh stalled") if identity == "/data/new" else None
        runtime = Runtime(identity, freshness_error=freshness_error)
        created.append(runtime)
        return runtime

    service, registry, _clock, stopped, _threads = make_service(
        factory=factory,
        resolver=lambda path: ResolvedWorkspace(identity=str(path.parent), working_subdirectory=path),
    )

    async def scenario() -> None:
        lease_id = await acquire(service)
        await service.activate_workspace(lease_id=lease_id, absolute_path="/data/old/work")
        prior = await service.binding_for(lease_id=lease_id)

        failed = await service.activate_workspace(lease_id=lease_id, absolute_path="/data/new/work")
        assert failed["ok"] is False
        failure = cast(Mapping[str, object], failed["error"])
        assert failure.get("code") == "TIMED_OUT"

        restored = await service.binding_for(lease_id=lease_id)
        assert restored == prior
        old_state = registry.runtime_state("/data/old")
        assert old_state is not None and old_state.reference_count == 1
        assert registry.runtime_state("/data/new") is None
        assert service._lifecycle.active_holders("/data/old") == 1
        assert service._lifecycle.active_holders("/data/new") == 0
        assert [runtime.identity for runtime in stopped] == ["/data/new"]

        await service.release_lease(lease_id=lease_id, immediate=False)
        old_state = registry.runtime_state("/data/old")
        assert old_state is not None and old_state.reference_count == 0

    run(scenario())


def test_failed_refresh_keeps_an_existing_warm_runtime_retained() -> None:
    service, registry, _clock, stopped, _threads = make_service(
        resolver=lambda path: ResolvedWorkspace(identity=str(path.parent), working_subdirectory=path)
    )

    async def scenario() -> None:
        warm_lease = await acquire(service, "warm")
        await service.activate_workspace(lease_id=warm_lease, absolute_path="/data/target/work")
        warm_runtime = (await service.binding_for(lease_id=warm_lease)).runtime
        await service.release_lease(lease_id=warm_lease, immediate=False)
        warm_state = registry.runtime_state("/data/target")
        assert warm_state is not None and warm_state.reference_count == 0

        lease_id = await acquire(service, "active")
        await service.activate_workspace(lease_id=lease_id, absolute_path="/data/old/work")
        prior = await service.binding_for(lease_id=lease_id)
        warm_runtime.freshness_error = TimeoutError("refresh stalled")

        failed = await service.activate_workspace(lease_id=lease_id, absolute_path="/data/target/next")
        assert failed["ok"] is False
        failure = cast(Mapping[str, object], failed["error"])
        assert failure.get("code") == "TIMED_OUT"

        assert await service.binding_for(lease_id=lease_id) == prior
        target_state = registry.runtime_state("/data/target")
        assert target_state is not None
        assert target_state.runtime is warm_runtime
        assert target_state.reference_count == 0
        assert stopped == []

    run(scenario())


def test_normal_release_retains_runtime_until_warm_grace_sweep() -> None:
    service, registry, clock, stopped, stop_threads = make_service()
    caller_thread = threading.get_ident()

    async def scenario() -> None:
        lease_id = await acquire(service)
        await service.activate_workspace(lease_id=lease_id, absolute_path="/data/project")
        released = await service.release_lease(lease_id=lease_id, immediate=False)
        assert released["grace_deadline"] == WARM_GRACE_SECONDS
        assert registry.runtime_state("/data/project") is not None
        assert stopped == []

        clock.advance(WARM_GRACE_SECONDS)
        await service.sweep()
        assert registry.runtime_state("/data/project") is None

    run(scenario())
    assert [runtime.identity for runtime in stopped] == ["/data/project"]
    assert stop_threads != [caller_thread]


def test_immediate_release_stops_only_when_last_same_root_holder_leaves() -> None:
    service, registry, _clock, stopped, _threads = make_service()

    async def scenario() -> None:
        first = await acquire(service, "a")
        second = await acquire(service, "b")
        await service.activate_workspace(lease_id=first, absolute_path="/data/project")
        await service.activate_workspace(lease_id=second, absolute_path="/data/project")

        first_release = await service.release_lease(lease_id=first, immediate=True)
        assert first_release["active_holders"] == 1
        assert first_release["runtime_stopped"] is False
        assert first_release["runtime_stop_pending"] is False
        assert stopped == []

        second_release = await service.release_lease(lease_id=second, immediate=True)
        assert second_release["active_holders"] == 0
        assert second_release["runtime_stopped"] is True
        assert second_release["runtime_stop_pending"] is False
        assert registry.runtime_state("/data/project") is None

    run(scenario())
    assert [runtime.identity for runtime in stopped] == ["/data/project"]


def test_detached_runtime_stop_failure_remains_owned_until_later_sweep() -> None:
    """A failed detached stop is retried best-effort and never re-raises.

    The lease/registry binding is already released before the stop is
    attempted, so a stop failure must not surface as a bare exception out of
    ``release_lease`` -- only ``daemon_idle``/``migration_status`` may reflect
    the still-pending cleanup (finding 1).
    """

    clock = FakeClock()
    registry = WorkspaceRuntimeRegistry[str, Runtime, UUID](lambda identity: Runtime(identity))
    attempts: list[Runtime] = []

    def reject_once(runtime: Runtime) -> None:
        attempts.append(runtime)
        if len(attempts) == 1:
            raise RuntimeError("cleanup admission rejected")

    service = WorkspaceDaemonService[str, Runtime](
        lifecycle=LeaseLifecycle[str, Runtime](clock=clock),
        registry=registry,
        resolver=resolution,
        runtime_stopper=reject_once,
    )

    async def scenario() -> None:
        lease_id = await acquire(service, "owner")
        await service.activate_workspace(lease_id=lease_id, absolute_path="/data/project")

        released = await service.release_lease(lease_id=lease_id, immediate=True)
        assert released["runtime_stopped"] is False
        assert released["runtime_stop_pending"] is True

        assert registry.runtime_state("/data/project") is None
        assert service.daemon_idle() is False
        assert (await service.migration_status())["daemon_idle"] is False

        await service.sweep()
        assert service.daemon_idle() is True
        assert (await service.migration_status())["daemon_idle"] is True

    run(scenario())
    assert [runtime.identity for runtime in attempts] == ["/data/project", "/data/project"]


def test_release_workspace_reports_pending_stop_truthfully_on_failure() -> None:
    """``release_workspace`` must not claim success when its own stop fails."""

    clock = FakeClock()
    registry = WorkspaceRuntimeRegistry[str, Runtime, UUID](lambda identity: Runtime(identity))

    def always_fail(_runtime: Runtime) -> None:
        raise RuntimeError("cleanup wedged")

    service = WorkspaceDaemonService[str, Runtime](
        lifecycle=LeaseLifecycle[str, Runtime](clock=clock),
        registry=registry,
        resolver=resolution,
        runtime_stopper=always_fail,
    )

    async def scenario() -> None:
        lease_id = await acquire(service, "owner")
        await service.activate_workspace(lease_id=lease_id, absolute_path="/data/project")

        released = await service.release_workspace(lease_id=lease_id, immediate=True)
        assert released["runtime_stopped"] is False
        assert released["runtime_stop_pending"] is True
        assert service.daemon_idle() is False
        assert (await service.migration_status())["daemon_idle"] is False

    run(scenario())


def test_wedged_pending_cleanup_never_raises_and_does_not_poison_other_root() -> None:
    """An always-failing detached stop stays best-effort forever.

    Retrying it from an unrelated root's release must not raise, and that
    unrelated release/activation must succeed normally (finding 1).
    """

    clock = FakeClock()
    registry = WorkspaceRuntimeRegistry[str, Runtime, UUID](lambda identity: Runtime(identity))

    def always_fail(_runtime: Runtime) -> None:
        raise RuntimeError("cleanup wedged")

    service = WorkspaceDaemonService[str, Runtime](
        lifecycle=LeaseLifecycle[str, Runtime](clock=clock),
        registry=registry,
        resolver=resolution,
        runtime_stopper=always_fail,
    )

    async def scenario() -> None:
        wedged_lease = await acquire(service, "wedged")
        await service.activate_workspace(lease_id=wedged_lease, absolute_path="/data/wedged")
        await service.release_lease(lease_id=wedged_lease, immediate=True)
        assert service.daemon_idle() is False

        for _ in range(3):
            await service.sweep()
            assert service.daemon_idle() is False

        other_lease = await acquire(service, "clean")
        activation = await service.activate_workspace(lease_id=other_lease, absolute_path="/data/other")
        workspace = activation["workspace"]
        assert isinstance(workspace, Mapping)
        assert cast(Mapping[str, object], workspace)["identity"] == "/data/other"

        other_release = await service.release_lease(lease_id=other_lease, immediate=True)
        # The stopper fails unconditionally, so this call's own target is
        # truthfully reported as still pending -- not falsely "stopped" -- but
        # the call itself completed normally instead of raising.
        assert other_release["runtime_stopped"] is False
        assert other_release["runtime_stop_pending"] is True
        assert registry.runtime_state("/data/other") is None
        # The wedged root's cleanup is still pending -- an unrelated root's
        # own operations succeeded without ever observing its failure.
        assert service.daemon_idle() is False

    run(scenario())


def test_periodic_sweep_survives_an_always_failing_pending_stop() -> None:
    """A wedged runtime must not terminate ``_sweep_periodically`` (finding 1)."""

    clock = FakeClock()
    registry = WorkspaceRuntimeRegistry[str, Runtime, UUID](lambda identity: Runtime(identity))

    def always_fail(_runtime: Runtime) -> None:
        raise RuntimeError("cleanup wedged")

    service = WorkspaceDaemonService[str, Runtime](
        lifecycle=LeaseLifecycle[str, Runtime](clock=clock),
        registry=registry,
        resolver=resolution,
        runtime_stopper=always_fail,
    )

    async def scenario() -> None:
        lease_id = await acquire(service, "wedged")
        await service.activate_workspace(lease_id=lease_id, absolute_path="/data/wedged")
        await service.release_lease(lease_id=lease_id, immediate=True)
        assert service.daemon_idle() is False

        sweep_task = asyncio.create_task(
            cli._sweep_periodically(service, 0.001, idle_exit_seconds=0.05)
        )
        try:
            for _ in range(5):
                await asyncio.sleep(0.005)
                assert not sweep_task.done()
        finally:
            sweep_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await sweep_task

    run(scenario())


def test_sweep_releases_expired_binding_then_stops_after_grace() -> None:
    service, registry, clock, stopped, _threads = make_service()

    async def scenario() -> None:
        lease_id = await acquire(service)
        await service.activate_workspace(lease_id=lease_id, absolute_path="/data/project")

        clock.advance(LEASE_EXPIRY_SECONDS)
        expired = await service.sweep()
        assert len(expired) == 1
        state = registry.runtime_state("/data/project")
        assert state is not None and state.reference_count == 0
        assert stopped == []

        clock.advance(WARM_GRACE_SECONDS)
        grace = await service.sweep()
        assert len(grace) == 1
        assert registry.runtime_state("/data/project") is None

    run(scenario())
    assert [runtime.identity for runtime in stopped] == ["/data/project"]


def test_unknown_or_expired_authority_raises_transport_typed_lease_expired() -> None:
    service, _registry, clock, _stopped, _threads = make_service()

    async def scenario() -> None:
        with pytest.raises(LeaseExpiredError):
            await service.heartbeat(lease_id="not-a-uuid")

        lease_id = await acquire(service)
        clock.advance(LEASE_EXPIRY_SECONDS)
        with pytest.raises(LeaseExpiredError):
            await service.heartbeat(lease_id=lease_id)
        with pytest.raises(LeaseExpiredError):
            await service.activate_workspace(lease_id=lease_id, absolute_path="/data/project")

    run(scenario())
