from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from uuid import uuid4

import anyio
from mcp import ClientSession, types
from mcp.server import Server
from mcp.shared.message import SessionMessage

from serena_light.connector import (
    ACQUIRE_LEASE_TOOL,
    GET_DAEMON_STATUS_TOOL,
    HEARTBEAT_TOOL,
    RELEASE_LEASE_TOOL,
    Connector,
    DaemonEndpoint,
    DaemonSession,
    McpDaemonSession,
    build_proxy_server,
)
from serena_light.instructions import AGENT_INSTRUCTIONS
from serena_light.runtime_files import BearerSecret


def ok_result(**data: object) -> types.CallToolResult:
    payload = {"ok": True, "data": data}
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=str(data))],
        structuredContent=payload,
    )


def tool(name: str) -> types.Tool:
    return types.Tool(name=name, description=name, inputSchema={"type": "object"})


async def run_server(server: Server, read_stream, write_stream) -> None:
    await server.run(read_stream, write_stream, server.create_initialization_options(), raise_exceptions=True)


def channels():
    client_send, server_receive = anyio.create_memory_object_stream[SessionMessage](0)
    server_send, client_receive = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    return client_receive, client_send, server_receive, server_send


class StaticDiscovery:
    def __init__(self, endpoint: DaemonEndpoint) -> None:
        self.endpoint = endpoint

    async def discover(self) -> DaemonEndpoint:
        return self.endpoint


class OneSessionFactory:
    def __init__(self, session: McpDaemonSession) -> None:
        self.session = session
        self.calls = 0

    async def connect(self, endpoint: DaemonEndpoint) -> DaemonSession:
        del endpoint
        self.calls += 1
        return self.session


def test_real_in_process_mcp_server_proxies_tools_and_preserves_structured_results() -> None:
    async def scenario() -> None:
        daemon_id = str(uuid4())
        lease_id = str(uuid4())
        upstream = Server("in-process-daemon", version="0.1.0")
        observed_meta: list[dict[str, object]] = []
        releases: list[str] = []

        @upstream.list_tools()
        async def list_upstream_tools() -> list[types.Tool]:
            return [
                tool(GET_DAEMON_STATUS_TOOL),
                tool(ACQUIRE_LEASE_TOOL),
                tool(HEARTBEAT_TOOL),
                tool(RELEASE_LEASE_TOOL),
                tool("activate_workspace"),
                tool("echo"),
            ]

        async def call_upstream_tool(request: types.CallToolRequest) -> types.ServerResult:
            name = request.params.name
            arguments = request.params.arguments or {}
            if name == GET_DAEMON_STATUS_TOOL:
                result = ok_result(daemon_id=daemon_id)
            elif name == ACQUIRE_LEASE_TOOL:
                result = ok_result(lease_id=lease_id, daemon_id=daemon_id)
            elif name == HEARTBEAT_TOOL:
                result = ok_result(renewed=True)
            elif name == RELEASE_LEASE_TOOL:
                releases.append(str(arguments["lease_id"]))
                result = ok_result(released=True)
            elif name == "activate_workspace":
                result = ok_result(absolute_path=arguments["absolute_path"])
            elif name == "echo":
                meta = request.params.meta
                assert meta is not None
                dumped = meta.model_dump(exclude_none=True)
                observed_meta.append(dumped)
                result = ok_result(echo=arguments["value"])
            else:  # pragma: no cover - test server invariant
                raise AssertionError(name)
            return types.ServerResult(result)

        upstream.request_handlers[types.CallToolRequest] = call_upstream_tool

        upstream_client_receive, upstream_client_send, upstream_server_receive, upstream_server_send = channels()
        upstream_task = asyncio.create_task(
            run_server(upstream, upstream_server_receive, upstream_server_send),
            name="in-process-upstream-mcp",
        )

        downstream_task: asyncio.Task[None] | None = None
        connector: Connector | None = None
        try:
            async with ClientSession(upstream_client_receive, upstream_client_send) as upstream_client:
                await upstream_client.initialize()
                sdk_session = McpDaemonSession(upstream_client, AsyncExitStack())
                endpoint = DaemonEndpoint(
                    daemon_id=daemon_id,
                    url="http://127.0.0.1:12345/mcp",
                    bearer=BearerSecret("x" * 48),
                    protocol_version=types.LATEST_PROTOCOL_VERSION,
                    server_version="0.1.0",
                )
                connector = Connector(
                    StaticDiscovery(endpoint),
                    OneSessionFactory(sdk_session),
                    startup_cwd=Path("/data/CoordExp"),
                )
                downstream = build_proxy_server(connector)
                downstream_client_receive, downstream_client_send, downstream_server_receive, downstream_server_send = (
                    channels()
                )
                downstream_task = asyncio.create_task(
                    run_server(downstream, downstream_server_receive, downstream_server_send),
                    name="in-process-stdio-proxy",
                )
                async with ClientSession(downstream_client_receive, downstream_client_send) as downstream_client:
                    initialized = await downstream_client.initialize()
                    assert initialized.instructions == AGENT_INSTRUCTIONS
                    listed = await downstream_client.list_tools()
                    assert [item.name for item in listed.tools] == ["activate_workspace", "echo"]
                    echoed = await downstream_client.call_tool("echo", {"value": "through-proxy"})
                    assert echoed.structuredContent == {"ok": True, "data": {"echo": "through-proxy"}}

                await connector.aclose()
                connector = None
                assert releases == [lease_id]
                assert observed_meta == [{"serena_light": {"lease_id": lease_id}}]
        finally:
            if connector is not None:
                await connector.aclose()
            for task in (downstream_task, upstream_task):
                if task is not None:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    asyncio.run(scenario())
