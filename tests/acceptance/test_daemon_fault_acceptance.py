"""Bounded real-boundary acceptance for daemon lifecycle and fault recovery.

These tests intentionally keep deterministic blocking and crash placement in a
test-only runtime.  The daemon process, loopback Streamable HTTP server,
production connector, 15-second heartbeats, lease service, executor, and
parent-death launcher are all the current production components.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import cast
from uuid import uuid4

import psutil
import pytest
from mcp import types
from mcp.types import LATEST_PROTOCOL_VERSION

import serena_light.connector as connector_module
from serena_light import __version__
from serena_light.connector import Connector, DaemonEndpoint, McpSessionFactory
from serena_light.daemon.server import LOOPBACK_HOST, spawn_detached_process
from serena_light.runtime_files import BearerSecret

pytestmark = pytest.mark.timeout(105)

_DRIVER = Path(__file__).with_name("daemon_fault_driver.py")


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind((LOOPBACK_HOST, 0))
        return int(listener.getsockname()[1])


def _events(state_path: Path) -> list[dict[str, object]]:
    if not state_path.exists():
        return []
    return [json.loads(line) for line in state_path.read_text(encoding="utf-8").splitlines() if line]


def _wait_for_event(state_path: Path, name: str, *, after: int = 0, timeout: float = 10.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = [event for event in _events(state_path)[after:] if event["event"] == name]
        if matches:
            return matches[-1]
        time.sleep(0.02)
    raise TimeoutError(f"did not observe {name} in {state_path}")


def _live_identity(pid: int, create_time: float) -> bool:
    try:
        process = psutil.Process(pid)
        return (
            process.is_running() and process.status() != psutil.STATUS_ZOMBIE and process.create_time() == create_time
        )
    except psutil.Error:
        return False


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    create_time: float
    process_group: int


class DynamicDiscovery:
    def __init__(self, endpoint: DaemonEndpoint) -> None:
        self.endpoint = endpoint

    async def discover(self) -> DaemonEndpoint:
        return self.endpoint


class ConnectorActor:
    """Keep each Streamable HTTP client's AnyIO scopes in one owning task."""

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
        self._task = asyncio.create_task(self._run())
        await self._started

    async def call_tool(self, name: str, arguments: Mapping[str, object] | None = None) -> types.CallToolResult:
        return await self._submit(lambda connector: connector.call_tool(name, arguments))  # type: ignore[return-value]

    async def lease_id(self) -> str | None:
        return await self._submit(lambda connector: _value(connector.lease_id))  # type: ignore[return-value]

    async def recovery_state(self) -> tuple[Path | None, str | None, str | None]:
        return await self._submit(
            lambda connector: _value(
                (
                    connector.last_validated_binding,
                    connector.lease_id,
                    None if connector._endpoint is None else connector._endpoint.daemon_id,
                )
            )
        )  # type: ignore[return-value]

    async def aclose(self) -> None:
        if self._task is None:
            return
        task, self._task = self._task, None
        await self._commands.put(None)
        await task

    async def _submit(self, callback: Callable[[Connector], Awaitable[object]]) -> object:
        assert self._task is not None
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        await self._commands.put((callback, future))
        return await future

    async def _run(self) -> None:
        assert self._started is not None
        try:
            await self._connector.start()
        except BaseException as exc:
            self._started.set_exception(exc)
            return
        self._started.set_result(None)
        try:
            while (command := await self._commands.get()) is not None:
                callback, future = command
                try:
                    future.set_result(await callback(self._connector))
                except BaseException as exc:
                    future.set_exception(exc)
        finally:
            await self._connector.aclose()


async def _value(value: object) -> object:
    return value


def _connector(discovery: DynamicDiscovery, root: Path) -> ConnectorActor:
    return ConnectorActor(Connector(discovery, McpSessionFactory(connect_timeout_seconds=5.0), startup_cwd=root))


class DaemonProcess:
    def __init__(self, *, tmp_path: Path, root: Path, block_crash_operations: bool) -> None:
        self.state_path = tmp_path / f"daemon-{uuid4()}.jsonl"
        self.root = root
        self.token = "a" * 48
        self.daemon_id = str(uuid4())
        self.port = _free_port()
        self._identity: ProcessIdentity | None = None
        self._block_crash_operations = block_crash_operations

    @property
    def endpoint(self) -> DaemonEndpoint:
        return DaemonEndpoint(
            daemon_id=self.daemon_id,
            url=f"http://{LOOPBACK_HOST}:{self.port}/mcp",
            bearer=BearerSecret(self.token),
            protocol_version=LATEST_PROTOCOL_VERSION,
            server_version=__version__,
        )

    @property
    def identity(self) -> ProcessIdentity:
        assert self._identity is not None
        return self._identity

    def start(self) -> None:
        argv = [
            sys.executable,
            os.fspath(_DRIVER),
            "--state",
            os.fspath(self.state_path),
            "--root",
            os.fspath(self.root),
            "--port",
            str(self.port),
            "--token",
            self.token,
            "--daemon-id",
            self.daemon_id,
        ]
        if self._block_crash_operations:
            argv.append("--block-crash-operations")
        process = spawn_detached_process(argv, cwd=Path.cwd(), env=dict(os.environ))
        attempt: ProcessIdentity | None = None
        try:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                try:
                    candidate = psutil.Process(process.pid)
                    attempt = ProcessIdentity(process.pid, candidate.create_time(), os.getpgid(process.pid))
                    _wait_for_event(self.state_path, "ready", timeout=0.1)
                    self._assert_healthy()
                    self._identity = attempt
                    return
                except (psutil.Error, OSError, TimeoutError, urllib.error.URLError):
                    time.sleep(0.02)
            raise TimeoutError("detached acceptance daemon did not become healthy")
        except BaseException:
            self._reclaim_failed_start(process.pid, attempt)
            raise

    def owned_descendants(self) -> tuple[ProcessIdentity, ...]:
        process = psutil.Process(self.identity.pid)
        return tuple(
            ProcessIdentity(child.pid, child.create_time(), os.getpgid(child.pid))
            for child in process.children(recursive=True)
            if child.status() != psutil.STATUS_ZOMBIE
        )

    def kill(self) -> None:
        identity = self.identity
        assert _live_identity(identity.pid, identity.create_time), "refusing to signal a reused daemon PID"
        os.kill(identity.pid, signal.SIGKILL)

    def close(self) -> None:
        if self._identity is None:
            return
        identity = self._identity
        if not _live_identity(identity.pid, identity.create_time):
            return
        os.kill(identity.pid, signal.SIGTERM)
        deadline = time.monotonic() + 5.0
        while _live_identity(identity.pid, identity.create_time) and time.monotonic() < deadline:
            time.sleep(0.02)
        if _live_identity(identity.pid, identity.create_time):
            os.kill(identity.pid, signal.SIGKILL)

    @staticmethod
    def _reclaim_failed_start(pid: int, identity: ProcessIdentity | None) -> None:
        """Terminate the detached process group created by a failed start attempt."""

        try:
            process_group = os.getpgid(pid) if identity is None else identity.process_group
        except OSError:
            return
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if identity is None or not _live_identity(identity.pid, identity.create_time):
                return
            time.sleep(0.02)
        with suppress(ProcessLookupError):
            os.killpg(process_group, signal.SIGKILL)

    def _assert_healthy(self) -> None:
        request = urllib.request.Request(
            self.endpoint.url.removesuffix("/mcp") + "/health",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=1.0) as response:
            payload = json.load(response)
        assert payload["data"]["daemon_id"] == self.daemon_id


def _payload(result: types.CallToolResult) -> Mapping[str, object]:
    assert isinstance(result.structuredContent, Mapping)
    return cast(Mapping[str, object], result.structuredContent)


def _mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return cast(Mapping[str, object], value)


async def _wait_for_count(
    state_path: Path, event: str, count: int, *, timeout: float = 10.0
) -> list[dict[str, object]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = [item for item in _events(state_path) if item["event"] == event]
        if len(matches) >= count:
            return matches
        await asyncio.sleep(0.02)
    raise TimeoutError(f"did not observe {count} {event} events")


def test_failed_daemon_start_reclaims_its_detached_process_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    daemon = DaemonProcess(tmp_path=tmp_path, root=tmp_path / "workspace", block_crash_operations=False)

    def fail_health() -> None:
        raise RuntimeError("forced acceptance startup failure")

    monkeypatch.setattr(daemon, "_assert_healthy", fail_health)

    with pytest.raises(RuntimeError, match="forced acceptance startup failure"):
        daemon.start()

    ready = _wait_for_event(daemon.state_path, "ready")
    ready_pid = ready["pid"]
    ready_create_time = ready["create_time"]
    assert isinstance(ready_pid, int)
    assert isinstance(ready_create_time, int | float)
    identity = ProcessIdentity(
        pid=ready_pid,
        create_time=float(ready_create_time),
        process_group=ready_pid,
    )
    deadline = time.monotonic() + 5.0
    while _live_identity(identity.pid, identity.create_time) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not _live_identity(identity.pid, identity.create_time), "failed start left its detached driver running"


@pytest.mark.timeout(100)
def test_real_wall_clock_block_keeps_production_heartbeats_status_and_second_root_responsive(tmp_path: Path) -> None:
    """5.9: a true >60 s operation crosses process, HTTP, connector, and lease boundaries."""

    async def scenario() -> None:
        daemon = DaemonProcess(tmp_path=tmp_path, root=tmp_path / "root-a", block_crash_operations=False)
        daemon.start()
        discovery = DynamicDiscovery(daemon.endpoint)
        first = _connector(discovery, daemon.root)
        observer = _connector(discovery, daemon.root)
        second_root = tmp_path / "root-b"
        second_root.mkdir()
        second = _connector(discovery, second_root)
        try:
            await first.start()
            await observer.start()
            await second.start()
            first_lease = await first.lease_id()
            assert first_lease is not None
            started_at = time.monotonic()
            blocked = asyncio.create_task(first.call_tool("find_symbol", {"name_path": "long"}))
            await asyncio.sleep(0.2)
            if blocked.done():
                await blocked
            await _wait_for_count(daemon.state_path, "long_read_started", 1)

            # These requests are real loopback HTTP/MCP calls while the first
            # workspace worker is blocked inside the daemon process.
            status = await asyncio.wait_for(observer.call_tool("get_runtime_status"), timeout=2.0)
            status_payload = _payload(status)
            assert status_payload["ok"] is True
            runtime = _mapping(_mapping(status_payload["data"])["runtime"])
            executor = _mapping(runtime["executor"])
            assert executor["active"] is True
            assert executor["actual_worker_count"] == 1
            assert executor["queue_capacity"] == 2

            other = await asyncio.wait_for(second.call_tool("find_symbol", {"name_path": "other-root"}), timeout=2.0)
            assert _payload(other)["ok"] is True
            completed = await blocked
            elapsed = time.monotonic() - started_at
            assert elapsed > 60.0
            assert _payload(completed)["ok"] is True

            heartbeats = [
                item
                for item in _events(daemon.state_path)
                if item["event"] == "heartbeat" and item["lease_id"] == first_lease
            ]
            assert len(heartbeats) >= 4
            cadence = [float(later["at"]) - float(earlier["at"]) for earlier, later in pairwise(heartbeats)]
            assert all(12.0 <= interval <= 20.0 for interval in cadence[:3])
        finally:
            await first.aclose()
            await observer.aclose()
            await second.aclose()
            daemon.close()

    asyncio.run(scenario())


def test_detached_daemon_survives_winner_connector_exit_while_second_lease_is_healthy(tmp_path: Path) -> None:
    """5.3: the daemon is detached and a normal connector exit does not own it."""

    async def scenario() -> None:
        daemon = DaemonProcess(tmp_path=tmp_path, root=tmp_path / "shared", block_crash_operations=False)
        daemon.start()
        discovery = DynamicDiscovery(daemon.endpoint)
        winner = _connector(discovery, daemon.root)
        retained = _connector(discovery, daemon.root)
        try:
            await winner.start()
            await retained.start()
            winner_lease, retained_lease = await winner.lease_id(), await retained.lease_id()
            assert winner_lease and retained_lease and winner_lease != retained_lease
            await winner.aclose()
            assert _live_identity(daemon.identity.pid, daemon.identity.create_time)
            retained_status = _payload(await retained.call_tool("get_runtime_status"))
            assert retained_status["ok"] is True
            assert await retained.lease_id() == retained_lease
        finally:
            await winner.aclose()
            await retained.aclose()
            daemon.close()

    asyncio.run(scenario())


@pytest.mark.timeout(50)
def test_sigkill_idle_read_and_edit_cleanup_rebind_and_never_replay_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5.10 / 9.7: SIGKILL leaves no owned child and connector recovery is safe."""

    # Containment rejects edits before a daemon call. Exercise the retained
    # recovery core directly so restoring advertisement cannot reintroduce
    # replay after an unknown edit outcome.
    monkeypatch.setattr(connector_module, "WITHHELD_TOOLS", frozenset())

    async def phase(name: str) -> None:
        root = tmp_path / name / "workspace"
        root.mkdir(parents=True)
        old = DaemonProcess(tmp_path=tmp_path / name, root=root, block_crash_operations=True)
        old.state_path.parent.mkdir(parents=True, exist_ok=True)
        old.start()
        discovery = DynamicDiscovery(old.endpoint)
        connector = _connector(discovery, root)
        pending: asyncio.Task[types.CallToolResult] | None = None
        try:
            await connector.start()
            # Connector startup is intentionally transport-only.  The first
            # workspace-dependent call binds inherited cwd and starts the
            # adapter-owned child whose parent-death cleanup is under test.
            initial_status = await connector.call_tool("get_runtime_status")
            assert _payload(initial_status)["ok"] is True
            before = old.owned_descendants()
            assert before, "activation must launch an owned parent-death child before SIGKILL"
            assert all(item.process_group == item.pid for item in before)
            if name == "read":
                pending = asyncio.create_task(connector.call_tool("find_symbol", {"name_path": "crash-read"}))
                await _wait_for_count(old.state_path, "crash_read_started", 1)
            elif name == "edit":
                pending = asyncio.create_task(
                    connector.call_tool(
                        "replace_symbol_body",
                        {
                            "name_path": "Example.method",
                            "relative_path": "example.py",
                            "body": "pass",
                            "expected_hash": "0" * 64,
                        },
                    )
                )
                await _wait_for_count(old.state_path, "edit_started", 1)

            old.kill()
            replacement = DaemonProcess(tmp_path=tmp_path / name, root=root, block_crash_operations=False)
            replacement.start()
            discovery.endpoint = replacement.endpoint
            try:
                for identity in (old.identity, *before):
                    deadline = time.monotonic() + 8.0
                    while _live_identity(identity.pid, identity.create_time) and time.monotonic() < deadline:
                        await asyncio.sleep(0.02)
                    assert not _live_identity(identity.pid, identity.create_time), (
                        f"owned pid {identity.pid} create_time {identity.create_time} survived daemon SIGKILL"
                    )

                if pending is None:
                    result = await connector.call_tool("get_runtime_status")
                    assert _payload(result)["ok"] is True
                else:
                    result = await asyncio.wait_for(pending, timeout=12.0)
                if name == "edit":
                    payload = _payload(result)
                    assert payload["ok"] is False
                    error = _mapping(payload["error"])
                    assert error["code"] == "UNCERTAIN"
                    details = _mapping(error["details"])
                    assert details["requires_current_reread"] is True
                    assert len([item for item in _events(old.state_path) if item["event"] == "edit_started"]) == 1
                    reread = await connector.call_tool("find_symbol", {"name_path": "current-reread"})
                    assert _payload(reread)["ok"] is True
                else:
                    assert _payload(result)["ok"] is True
                binding, lease_id, daemon_id = await connector.recovery_state()
                assert binding == root.resolve()
                assert lease_id is not None
                assert daemon_id == replacement.daemon_id
            finally:
                replacement.close()
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
            await connector.aclose()
            old.close()

    async def scenario() -> None:
        for name in ("idle", "read", "edit"):
            await phase(name)

    asyncio.run(scenario())
