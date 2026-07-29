from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from uuid import uuid4

import pytest
from mcp import types

from serena_light.connector import (
    ACQUIRE_LEASE_TOOL,
    ACTIVATE_WORKSPACE_TOOL,
    GET_DAEMON_STATUS_TOOL,
    HEARTBEAT_TOOL,
    RELEASE_LEASE_TOOL,
    Connector,
    ConnectorRecoveryError,
    ConnectorSessionLost,
    DaemonEndpoint,
    DaemonSession,
    InvalidDaemonResponse,
    LeaseGrant,
)
from serena_light.runtime_files import BearerSecret


def endpoint(daemon_id: str | None = None) -> DaemonEndpoint:
    return DaemonEndpoint(
        daemon_id=daemon_id or str(uuid4()),
        url="http://127.0.0.1:9876/mcp",
        bearer=BearerSecret("x" * 48),
        protocol_version=types.LATEST_PROTOCOL_VERSION,
        server_version="0.1.0",
    )


def ok_result(**data: object) -> types.CallToolResult:
    payload = {"ok": True, "data": data}
    return types.CallToolResult(
        content=[types.TextContent(type="text", text="ok")],
        structuredContent=payload,
    )


def tool(name: str) -> types.Tool:
    return types.Tool(name=name, description=name, inputSchema={"type": "object"})


class FakeDiscovery:
    def __init__(self, current: DaemonEndpoint) -> None:
        self.current = current
        self.calls = 0

    async def discover(self) -> DaemonEndpoint:
        self.calls += 1
        return self.current


type CallBehavior = Callable[[str, Mapping[str, object] | None], Awaitable[types.CallToolResult]]


class FakeSession:
    def __init__(
        self,
        endpoint: DaemonEndpoint,
        *,
        call_behavior: CallBehavior | None = None,
        activation_error: BaseException | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.lease = LeaseGrant(str(uuid4()), endpoint.daemon_id)
        self.call_behavior = call_behavior
        self.activation_error = activation_error
        self.activations: list[tuple[str, Path]] = []
        self.call_names: list[str] = []
        self.heartbeats = 0
        self.released: list[str] = []
        self.closed = False
        self.tools = [
            tool(GET_DAEMON_STATUS_TOOL),
            tool(ACQUIRE_LEASE_TOOL),
            tool(HEARTBEAT_TOOL),
            tool(RELEASE_LEASE_TOOL),
            tool("find_symbol"),
            tool("replace_symbol_body"),
        ]

    async def acquire_lease(self) -> LeaseGrant:
        return self.lease

    async def heartbeat(self, lease_id: str) -> None:
        assert lease_id == self.lease.lease_id
        self.heartbeats += 1

    async def release_lease(self, lease_id: str) -> None:
        self.released.append(lease_id)

    async def activate_workspace(self, lease_id: str, path: Path) -> types.CallToolResult:
        assert lease_id == self.lease.lease_id
        self.activations.append((lease_id, path))
        if self.activation_error is not None:
            raise self.activation_error
        return ok_result(path=str(path))

    async def list_tools(self) -> types.ListToolsResult:
        return types.ListToolsResult(tools=self.tools)

    async def call_tool(
        self,
        lease_id: str,
        name: str,
        arguments: Mapping[str, object] | None,
    ) -> types.CallToolResult:
        assert lease_id == self.lease.lease_id
        self.call_names.append(name)
        if self.call_behavior is not None:
            return await self.call_behavior(name, arguments)
        return ok_result(name=name)

    async def aclose(self) -> None:
        self.closed = True


class FakeFactory:
    def __init__(self, sessions: list[FakeSession] | None = None) -> None:
        self.sessions = sessions or []
        self.connected: list[DaemonEndpoint] = []

    async def connect(self, endpoint: DaemonEndpoint) -> DaemonSession:
        self.connected.append(endpoint)
        if self.sessions:
            session = self.sessions.pop(0)
            assert session.endpoint == endpoint
            return session
        return FakeSession(endpoint)


def run(awaitable):
    return asyncio.run(awaitable)


def test_inherited_cwd_is_activated_once_and_later_shell_cd_is_not_observable(tmp_path: Path) -> None:
    async def scenario() -> None:
        inherited = tmp_path / "repo" / "nested"
        later = tmp_path / "other"
        inherited.mkdir(parents=True)
        later.mkdir()
        discovered = endpoint()
        session = FakeSession(discovered)
        connector = Connector(FakeDiscovery(discovered), FakeFactory([session]), startup_cwd=inherited)
        previous = Path.cwd()
        try:
            await connector.start()
            os.chdir(later)
            await connector.call_tool("find_symbol", {"name_path": "Thing"})
        finally:
            os.chdir(previous)
            await connector.aclose()

        assert connector.startup_cwd == inherited.resolve()
        assert connector.last_validated_binding == inherited.resolve()
        assert [path for _lease, path in session.activations] == [inherited.resolve()]

    run(scenario())


def test_two_connectors_reuse_discovered_daemon_have_distinct_leases_and_no_lsp_import() -> None:
    async def scenario() -> None:
        discovered = endpoint()
        discovery = FakeDiscovery(discovered)
        factory = FakeFactory()
        first = Connector(discovery, factory, startup_cwd=Path("/data/CoordExp"))
        second = Connector(discovery, factory, startup_cwd=Path("/data/CoordExp"))
        await first.start()
        await second.start()
        assert [item.daemon_id for item in factory.connected] == [discovered.daemon_id, discovered.daemon_id]
        assert first.lease_id != second.lease_id
        await first.aclose()
        await second.aclose()

    run(scenario())
    source = inspect.getsource(__import__("serena_light.connector", fromlist=["Connector"]))
    assert "serena_light.lsp" not in source


def test_control_plane_tools_are_not_agent_visible() -> None:
    async def scenario() -> None:
        discovered = endpoint()
        connector = Connector(FakeDiscovery(discovered), FakeFactory(), startup_cwd=Path("/data/CoordExp"))
        try:
            result = await connector.list_tools()
            assert [item.name for item in result.tools] == ["find_symbol", "replace_symbol_body"]
        finally:
            await connector.aclose()

    run(scenario())


def test_heartbeat_is_independent_of_a_blocked_tool_call() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def block(_name: str, _arguments: Mapping[str, object] | None) -> types.CallToolResult:
            started.set()
            await release.wait()
            return ok_result()

        discovered = endpoint()
        session = FakeSession(discovered, call_behavior=block)
        connector = Connector(
            FakeDiscovery(discovered),
            FakeFactory([session]),
            startup_cwd=Path("/data/CoordExp"),
            heartbeat_interval_seconds=0.01,
        )
        await connector.start()
        call = asyncio.create_task(connector.call_tool("find_symbol"))
        await started.wait()
        await asyncio.sleep(0.045)
        assert session.heartbeats >= 3
        release.set()
        await call
        await connector.aclose()

    run(scenario())


def test_normal_exit_releases_lease_and_closes_session() -> None:
    async def scenario() -> None:
        discovered = endpoint()
        session = FakeSession(discovered)
        connector = Connector(FakeDiscovery(discovered), FakeFactory([session]), startup_cwd=Path("/data/CoordExp"))
        await connector.start()
        lease_id = connector.lease_id
        await connector.aclose()
        await connector.aclose()
        assert session.released == [lease_id]
        assert session.closed

    run(scenario())


def test_identity_loss_rebinds_last_validated_binding_and_retries_read_once() -> None:
    async def scenario() -> None:
        first_endpoint = endpoint()
        second_endpoint = endpoint()
        discovery = FakeDiscovery(first_endpoint)

        async def lose(_name: str, _arguments: Mapping[str, object] | None) -> types.CallToolResult:
            discovery.current = second_endpoint
            raise ConnectorSessionLost("response lost")

        first = FakeSession(first_endpoint, call_behavior=lose)
        second = FakeSession(second_endpoint)
        connector = Connector(
            discovery,
            FakeFactory([first, second]),
            startup_cwd=Path("/data/CoordExp/serena-light"),
        )
        try:
            result = await connector.call_tool("find_symbol", {"name_path": "Connector"})
            assert result.structuredContent == {"ok": True, "data": {"name": "find_symbol"}}
            assert first.call_names == ["find_symbol"]
            assert second.call_names == ["find_symbol"]
            assert second.activations[0][1] == Path("/data/CoordExp/serena-light")
            assert connector.last_validated_binding == Path("/data/CoordExp/serena-light")
        finally:
            await connector.aclose()

    run(scenario())


def test_discovery_identity_change_rebinds_before_dispatching_the_read() -> None:
    async def scenario() -> None:
        first_endpoint = endpoint()
        second_endpoint = endpoint()
        discovery = FakeDiscovery(first_endpoint)
        first = FakeSession(first_endpoint)
        second = FakeSession(second_endpoint)
        connector = Connector(
            discovery,
            FakeFactory([first, second]),
            startup_cwd=Path("/data/CoordExp"),
        )
        await connector.start()
        discovery.current = second_endpoint
        try:
            result = await connector.call_tool("find_symbol")
            assert result.structuredContent == {"ok": True, "data": {"name": "find_symbol"}}
            assert first.call_names == []
            assert second.call_names == ["find_symbol"]
            assert second.activations[0][1] == Path("/data/CoordExp")
        finally:
            await connector.aclose()

    run(scenario())


def test_read_retry_is_attempted_at_most_once() -> None:
    async def scenario() -> None:
        discovered = endpoint()

        async def lose(_name: str, _arguments: Mapping[str, object] | None) -> types.CallToolResult:
            raise ConnectorSessionLost("lost")

        first = FakeSession(discovered, call_behavior=lose)
        second = FakeSession(discovered, call_behavior=lose)
        factory = FakeFactory([first, second])
        connector = Connector(FakeDiscovery(discovered), factory, startup_cwd=Path("/data/CoordExp"))
        try:
            with pytest.raises(ConnectorSessionLost, match="lost"):
                await connector.call_tool("find_symbol")
            assert len(factory.connected) == 2
            assert first.call_names == second.call_names == ["find_symbol"]
        finally:
            await connector.aclose()

    run(scenario())


def test_edit_loss_recovers_binding_but_returns_typed_uncertain_without_replay() -> None:
    async def scenario() -> None:
        first_endpoint = endpoint()
        second_endpoint = endpoint()
        discovery = FakeDiscovery(first_endpoint)

        async def lose(_name: str, _arguments: Mapping[str, object] | None) -> types.CallToolResult:
            discovery.current = second_endpoint
            raise ConnectorSessionLost("edit response lost")

        first = FakeSession(first_endpoint, call_behavior=lose)
        second = FakeSession(second_endpoint)
        connector = Connector(
            discovery,
            FakeFactory([first, second]),
            startup_cwd=Path("/data/CoordExp"),
        )
        try:
            result = await connector.call_tool("replace_symbol_body", {"expected_hash": "old"})
            assert result.isError
            assert result.structuredContent is not None
            assert result.structuredContent["error"]["code"] == "UNCERTAIN"
            assert result.structuredContent["error"]["retry"] == {"retryable": False}
            assert result.structuredContent["error"]["details"]["requires_current_reread"] is True
            assert first.call_names == ["replace_symbol_body"]
            assert second.call_names == []
            assert second.activations[0][1] == Path("/data/CoordExp")
        finally:
            await connector.aclose()

    run(scenario())


def test_failed_rebind_preserves_last_validated_binding_and_surfaces_failure() -> None:
    async def scenario() -> None:
        discovered = endpoint()

        async def lose(_name: str, _arguments: Mapping[str, object] | None) -> types.CallToolResult:
            raise ConnectorSessionLost("lost")

        first = FakeSession(discovered, call_behavior=lose)
        second = FakeSession(discovered, activation_error=InvalidDaemonResponse("binding rejected"))
        connector = Connector(
            FakeDiscovery(discovered),
            FakeFactory([first, second]),
            startup_cwd=Path("/data/CoordExp"),
        )
        await connector.start()
        old_lease = connector.lease_id
        with pytest.raises(ConnectorRecoveryError):
            await connector.call_tool("find_symbol")
        assert connector.last_validated_binding == Path("/data/CoordExp")
        assert connector.lease_id == old_lease
        assert second.closed
        assert second.released == [second.lease.lease_id]
        await connector.aclose()

    run(scenario())


def test_cancelled_call_does_not_prevent_connector_cleanup() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        never = asyncio.Event()

        async def block(_name: str, _arguments: Mapping[str, object] | None) -> types.CallToolResult:
            started.set()
            await never.wait()
            return ok_result()

        discovered = endpoint()
        session = FakeSession(discovered, call_behavior=block)
        connector = Connector(FakeDiscovery(discovered), FakeFactory([session]), startup_cwd=Path("/data/CoordExp"))
        await connector.start()
        call = asyncio.create_task(connector.call_tool("find_symbol"))
        await started.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        await connector.aclose()
        assert session.released == [session.lease.lease_id]
        assert session.closed

    run(scenario())


def test_successful_explicit_activation_updates_retained_binding_only_after_validation() -> None:
    async def scenario() -> None:
        discovered = endpoint()
        session = FakeSession(discovered)
        connector = Connector(FakeDiscovery(discovered), FakeFactory([session]), startup_cwd=Path("/data/CoordExp"))
        target = Path("/data/ms-swift")
        try:
            await connector.call_tool(ACTIVATE_WORKSPACE_TOOL, {"absolute_path": str(target)})
            assert connector.last_validated_binding == target
            with pytest.raises(InvalidDaemonResponse):
                await connector.call_tool(ACTIVATE_WORKSPACE_TOOL, {"absolute_path": "relative"})
            assert connector.last_validated_binding == target
        finally:
            await connector.aclose()

    run(scenario())
