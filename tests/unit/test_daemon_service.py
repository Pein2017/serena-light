from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from serena_light.daemon.leases import LEASE_EXPIRY_SECONDS, WARM_GRACE_SECONDS, LeaseLifecycle
from serena_light.daemon.server import LeaseExpiredError
from serena_light.daemon.service import WorkspaceDaemonService
from serena_light.workspace.registry import ResolvedWorkspace, WorkspaceRuntimeRegistry


def run[ResultT](coroutine: Coroutine[Any, Any, ResultT]) -> ResultT:
    return asyncio.run(coroutine)


@dataclass(eq=False, slots=True)
class Runtime:
    identity: str


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
    )
    return service, registry, service_clock, stopped, stop_threads


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
        assert stopped == []

        second_release = await service.release_lease(lease_id=second, immediate=True)
        assert second_release["active_holders"] == 0
        assert second_release["runtime_stopped"] is True
        assert registry.runtime_state("/data/project") is None

    run(scenario())
    assert [runtime.identity for runtime in stopped] == ["/data/project"]


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
