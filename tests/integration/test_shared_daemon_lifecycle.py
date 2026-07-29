from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import uvicorn
from mcp import types

from serena_light import __version__
from serena_light.connector import (
    ACTIVATE_WORKSPACE_TOOL,
    GET_DAEMON_STATUS_TOOL,
    Connector,
    DaemonEndpoint,
    McpSessionFactory,
)
from serena_light.daemon.leases import (
    LEASE_EXPIRY_SECONDS,
    WARM_GRACE_SECONDS,
    LeaseLifecycle,
    LeaseLifecycleDecision,
)
from serena_light.daemon.server import LOOPBACK_HOST, DaemonService, create_daemon_app
from serena_light.daemon.service import WorkspaceDaemonService
from serena_light.lsp.executor import BoundedLspExecutor, ExecutorBusyError
from serena_light.runtime_files import BearerSecret
from serena_light.workspace.registry import ResolvedWorkspace, WorkspaceRuntimeRegistry


@dataclass(slots=True)
class LockedClock:
    _now: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


@dataclass(eq=False, slots=True)
class Runtime:
    identity: str
    executor: BoundedLspExecutor
    stopped: bool = False

    def stop(self) -> None:
        self.executor.close()
        self.stopped = True


class ObservedService:
    """Observe real service calls without replacing any lifecycle behavior."""

    def __init__(self, service: WorkspaceDaemonService[str, Runtime]) -> None:
        self.service = service
        self.heartbeat_count = 0
        self.status_count = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._observation_lock = threading.Lock()

    async def status(self, *, mcp_session_id: str) -> Mapping[str, object]:
        self._remember_loop()
        with self._observation_lock:
            self.status_count += 1
        return await self.service.status(mcp_session_id=mcp_session_id)

    async def acquire_lease(self, *, mcp_session_id: str) -> Mapping[str, object]:
        self._remember_loop()
        return await self.service.acquire_lease(mcp_session_id=mcp_session_id)

    async def heartbeat(self, *, lease_id: str) -> Mapping[str, object]:
        self._remember_loop()
        result = await self.service.heartbeat(lease_id=lease_id)
        with self._observation_lock:
            self.heartbeat_count += 1
        return result

    async def release_lease(self, *, lease_id: str, immediate: bool) -> Mapping[str, object]:
        self._remember_loop()
        return await self.service.release_lease(lease_id=lease_id, immediate=immediate)

    async def activate_workspace(self, *, lease_id: str, absolute_path: str) -> Mapping[str, object]:
        self._remember_loop()
        return await self.service.activate_workspace(lease_id=lease_id, absolute_path=absolute_path)

    async def sweep(self) -> tuple[LeaseLifecycleDecision[str, Runtime], ...]:
        loop = self._loop
        assert loop is not None
        future = asyncio.run_coroutine_threadsafe(self.service.sweep(), loop)
        return await asyncio.wrap_future(future)

    def observed_heartbeats(self) -> int:
        with self._observation_lock:
            return self.heartbeat_count

    def _remember_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        else:
            assert self._loop is loop


@dataclass(slots=True)
class Harness:
    clock: LockedClock
    lifecycle: LeaseLifecycle[str, Runtime]
    registry: WorkspaceRuntimeRegistry[str, Runtime, UUID]
    service: ObservedService
    runtimes: dict[str, Runtime]


class StaticDiscovery:
    def __init__(self, endpoint: DaemonEndpoint) -> None:
        self.endpoint = endpoint

    async def discover(self) -> DaemonEndpoint:
        return self.endpoint


class ConnectorActor:
    """Keep one MCP client's AnyIO cancel scopes inside one owning task."""

    def __init__(self, connector: Connector) -> None:
        self._connector = connector
        self._commands: asyncio.Queue[
            tuple[Callable[[Connector], Awaitable[object]], asyncio.Future[object]] | None
        ] = asyncio.Queue()
        self._started: asyncio.Future[None] | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._started = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._run(), name="connector-actor")
        await self._started

    async def lease_id(self) -> str | None:
        return cast(str | None, await self._submit(lambda connector: _value(connector.lease_id)))

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, object] | None = None,
    ) -> types.CallToolResult:
        return cast(
            types.CallToolResult,
            await self._submit(lambda connector: connector.call_tool(name, arguments)),
        )

    async def cancel_heartbeats_without_release(self) -> None:
        await self._submit(_cancel_connector_heartbeats)

    async def aclose(self) -> None:
        task = self._task
        if task is None:
            return
        self._task = None
        await self._commands.put(None)
        await task

    async def _submit(self, call: Callable[[Connector], Awaitable[object]]) -> object:
        assert self._task is not None
        result: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        await self._commands.put((call, result))
        return await result

    async def _run(self) -> None:
        assert self._started is not None
        try:
            await self._connector.start()
        except BaseException as error:
            self._started.set_exception(error)
            return
        self._started.set_result(None)
        try:
            while (command := await self._commands.get()) is not None:
                call, result = command
                try:
                    value = await call(self._connector)
                except BaseException as error:
                    result.set_exception(error)
                else:
                    result.set_result(value)
        finally:
            await self._connector.aclose()


async def _value(value: object) -> object:
    return value


async def _cancel_connector_heartbeats(connector: Connector) -> object:
    heartbeat = connector._heartbeat_task
    assert heartbeat is not None
    connector._heartbeat_task = None
    heartbeat.cancel()
    with suppress(asyncio.CancelledError):
        await heartbeat
    return None


def _root_for(path: Path) -> str:
    resolved = path.resolve()
    for root in (Path("/data/root-a"), Path("/data/root-b"), Path("/data/shared")):
        if resolved == root or root in resolved.parents:
            return str(root)
    return str(resolved)


def _make_harness() -> Harness:
    clock = LockedClock()
    runtimes: dict[str, Runtime] = {}

    def runtime_factory(identity: str) -> Runtime:
        runtime = Runtime(
            identity=identity,
            executor=BoundedLspExecutor(queue_capacity=2, name=Path(identity).name),
        )
        runtimes[identity] = runtime
        return runtime

    def resolver(path: Path) -> ResolvedWorkspace[str]:
        return ResolvedWorkspace(identity=_root_for(path), working_subdirectory=path.resolve())

    lifecycle = LeaseLifecycle[str, Runtime](clock=clock)
    registry = WorkspaceRuntimeRegistry[str, Runtime, UUID](runtime_factory)
    concrete = WorkspaceDaemonService[str, Runtime](
        lifecycle=lifecycle,
        registry=registry,
        resolver=resolver,
        runtime_stopper=lambda runtime: runtime.stop(),
    )
    return Harness(
        clock=clock,
        lifecycle=lifecycle,
        registry=registry,
        service=ObservedService(concrete),
        runtimes=runtimes,
    )


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind((LOOPBACK_HOST, 0))
        return int(listener.getsockname()[1])


@contextmanager
def _running_daemon(harness: Harness) -> Iterator[DaemonEndpoint]:
    port = _free_loopback_port()
    daemon_id = str(uuid4())
    bearer = BearerSecret("integration-" + "s" * 48)
    app = create_daemon_app(
        service=cast(DaemonService, harness.service), bearer=bearer, daemon_id=daemon_id
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=LOOPBACK_HOST,
            port=port,
            log_level="critical",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, name="serena-light-test-daemon", daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("loopback daemon exited during startup")
        if time.monotonic() >= deadline:
            raise TimeoutError("loopback daemon did not start")
        time.sleep(0.01)
    try:
        yield DaemonEndpoint(
            daemon_id=daemon_id,
            url=f"http://{LOOPBACK_HOST}:{port}/mcp",
            bearer=bearer,
            protocol_version=types.LATEST_PROTOCOL_VERSION,
            server_version=__version__,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
        assert not thread.is_alive(), "loopback daemon did not stop"
        for runtime in harness.runtimes.values():
            if not runtime.stopped:
                runtime.stop()


def _connector(endpoint: DaemonEndpoint, root: str, *, heartbeat_interval: float = 0.02) -> ConnectorActor:
    return ConnectorActor(
        Connector(
            StaticDiscovery(endpoint),
            McpSessionFactory(connect_timeout_seconds=2.0),
            startup_cwd=Path(root),
            heartbeat_interval_seconds=heartbeat_interval,
        )
    )


def _data(result: types.CallToolResult) -> Mapping[str, object]:
    payload = result.structuredContent
    assert isinstance(payload, Mapping)
    assert payload["ok"] is True
    data = payload["data"]
    assert isinstance(data, Mapping)
    return cast(Mapping[str, object], data)


def _error_code(result: types.CallToolResult) -> str:
    payload = result.structuredContent
    assert isinstance(payload, Mapping)
    assert payload["ok"] is False
    error = payload["error"]
    assert isinstance(error, Mapping)
    code = error["code"]
    assert isinstance(code, str)
    return code


async def _wait_for_heartbeat_after(service: ObservedService, baseline: int) -> None:
    deadline = asyncio.get_running_loop().time() + 1.0
    while service.observed_heartbeats() <= baseline:
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("connector heartbeat did not traverse HTTP to the concrete service")
        await asyncio.sleep(0.01)


def _run[ResultT](coroutine: Coroutine[Any, Any, ResultT]) -> ResultT:
    return asyncio.run(coroutine)


def test_two_connectors_share_one_runtime_and_nonlast_exit_keeps_daemon_healthy() -> None:
    harness = _make_harness()
    with _running_daemon(harness) as endpoint:

        async def scenario() -> None:
            starter = _connector(endpoint, "/data/shared")
            retained = _connector(endpoint, "/data/shared/subdirectory")
            try:
                await starter.start()
                await retained.start()
                starter_lease = await starter.lease_id()
                retained_lease = await retained.lease_id()
                assert starter_lease is not None
                assert retained_lease is not None
                assert starter_lease != retained_lease

                state = harness.registry.runtime_state("/data/shared")
                assert state is not None
                assert state.reference_count == 2
                assert len(harness.runtimes) == 1
                assert harness.lifecycle.active_holders("/data/shared") == 2

                await starter.aclose()
                state = harness.registry.runtime_state("/data/shared")
                assert state is not None
                assert state.reference_count == 1
                assert state.runtime is harness.runtimes["/data/shared"]
                assert state.runtime.stopped is False
                assert harness.lifecycle.active_holders("/data/shared") == 1

                status = _data(await retained.call_tool(GET_DAEMON_STATUS_TOOL))
                assert status["daemon_id"] == endpoint.daemon_id
                assert await retained.lease_id() == retained_lease
            finally:
                await starter.aclose()
                await retained.aclose()

        _run(scenario())


def test_crashed_connector_expires_without_rebind_then_runtime_stops_after_grace() -> None:
    harness = _make_harness()
    with _running_daemon(harness) as endpoint:

        async def scenario() -> None:
            connector = _connector(endpoint, "/data/root-a")
            try:
                await connector.start()
                crashed_lease = await connector.lease_id()
                assert crashed_lease is not None
                await connector.cancel_heartbeats_without_release()

                harness.clock.advance(LEASE_EXPIRY_SECONDS)
                expired = await harness.service.sweep()
                assert len(expired) == 1
                retained = harness.registry.runtime_state("/data/root-a")
                assert retained is not None
                assert retained.reference_count == 0
                assert retained.runtime.stopped is False

                rejected = await connector.call_tool(
                    ACTIVATE_WORKSPACE_TOOL,
                    {"absolute_path": "/data/root-b"},
                )
                assert _error_code(rejected) == "LEASE_EXPIRED"
                assert await connector.lease_id() == crashed_lease
                assert "/data/root-b" not in harness.runtimes

                harness.clock.advance(WARM_GRACE_SECONDS)
                grace = await harness.service.sweep()
                assert len(grace) == 1
                assert harness.registry.runtime_state("/data/root-a") is None
                assert harness.runtimes["/data/root-a"].stopped is True
            finally:
                await connector.aclose()

        _run(scenario())


def test_logical_long_request_keeps_real_http_heartbeats_status_and_other_root_responsive() -> None:
    harness = _make_harness()
    with _running_daemon(harness) as endpoint:

        async def scenario() -> None:
            first = _connector(endpoint, "/data/root-a", heartbeat_interval=0.01)
            second = _connector(endpoint, "/data/root-b", heartbeat_interval=0.01)
            release = threading.Event()
            started = threading.Event()

            def blocking_request() -> str:
                started.set()
                assert release.wait(timeout=5.0)
                return "first-done"

            try:
                await first.start()
                await second.start()
                first_runtime = harness.runtimes["/data/root-a"]
                second_runtime = harness.runtimes["/data/root-b"]
                blocked = first_runtime.executor.submit(blocking_request)
                assert await asyncio.to_thread(started.wait, 1.0)

                queued_one = first_runtime.executor.submit(lambda: "queued-one")
                queued_two = first_runtime.executor.submit(lambda: "queued-two")
                with pytest.raises(ExecutorBusyError):
                    first_runtime.executor.submit(lambda: "must-not-grow")
                snapshot = first_runtime.executor.snapshot()
                assert snapshot.active is True
                assert snapshot.queue_size == snapshot.queue_capacity == 2
                assert sum(
                    thread.name == "serena-light-lsp:root-a" for thread in threading.enumerate()
                ) == 1

                baseline_heartbeats = harness.service.observed_heartbeats()
                logical_start = harness.clock()
                for _step in range(7):
                    harness.clock.advance(10.0)
                    await _wait_for_heartbeat_after(harness.service, baseline_heartbeats)
                    baseline_heartbeats = harness.service.observed_heartbeats()
                assert harness.clock() - logical_start > LEASE_EXPIRY_SECONDS

                started_at = time.monotonic()
                status = _data(
                    await asyncio.wait_for(first.call_tool(GET_DAEMON_STATUS_TOOL), timeout=0.5)
                )
                assert status["daemon_id"] == endpoint.daemon_id
                assert time.monotonic() - started_at < 0.5

                switched = _data(
                    await asyncio.wait_for(
                        second.call_tool(
                            ACTIVATE_WORKSPACE_TOOL,
                            {"absolute_path": "/data/root-b/subdirectory"},
                        ),
                        timeout=0.5,
                    )
                )
                assert cast(Mapping[str, object], switched["workspace"])["identity"] == "/data/root-b"
                assert await asyncio.wait_for(
                    asyncio.wrap_future(second_runtime.executor.submit(lambda: "second-done")),
                    timeout=0.5,
                ) == "second-done"

                assert await harness.service.sweep() == ()
                assert harness.lifecycle.active_holders("/data/root-a") == 1
                assert first_runtime.executor.snapshot().active is True

                release.set()
                assert await asyncio.wait_for(asyncio.wrap_future(blocked), timeout=0.5) == "first-done"
                assert await asyncio.wait_for(asyncio.wrap_future(queued_one), timeout=0.5) == "queued-one"
                assert await asyncio.wait_for(asyncio.wrap_future(queued_two), timeout=0.5) == "queued-two"
            finally:
                release.set()
                await first.aclose()
                await second.aclose()

        _run(scenario())
