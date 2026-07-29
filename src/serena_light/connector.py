"""Per-client stdio MCP proxy for the shared serena-light daemon.

This module deliberately has no workspace-runtime or language-server imports.
It owns only connector-local state: the inherited startup directory, one
daemon-issued lease, its independent heartbeat, and recovery of the HTTP MCP
session that carries requests to the shared daemon.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Self

import anyio
import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.stdio import stdio_server

from serena_light import __version__
from serena_light.runtime_files import (
    LEGACY_BUILD_IDENTITY,
    RUNTIME_ROOT,
    BearerSecret,
    DiscoveryMetadata,
    ProcessIdentityValidator,
    RuntimeFileError,
    read_bearer_secret,
    read_discovery_metadata,
)
from serena_light.tools.envelopes import ErrorCode, RetryMetadata, error

HEARTBEAT_INTERVAL_SECONDS = 15.0
CONNECTOR_NAME = "serena-light"
CONNECTOR_VERSION = __version__

GET_DAEMON_STATUS_TOOL = "get_daemon_status"
ACQUIRE_LEASE_TOOL = "acquire_lease"
HEARTBEAT_TOOL = "heartbeat"
RELEASE_LEASE_TOOL = "release_lease"
CONTROL_PLANE_TOOLS = frozenset(
    {
        GET_DAEMON_STATUS_TOOL,
        ACQUIRE_LEASE_TOOL,
        HEARTBEAT_TOOL,
        RELEASE_LEASE_TOOL,
    }
)

ACTIVATE_WORKSPACE_TOOL = "activate_workspace"
RELEASE_WORKSPACE_TOOL = "release_workspace"
EDIT_TOOLS = frozenset({"replace_symbol_body"})
WITHHELD_TOOLS: frozenset[str] = frozenset()
READ_ONLY_TOOLS = frozenset(
    {
        "get_runtime_status",
        "get_symbols_overview",
        "find_symbol",
        "find_referencing_symbols",
        "find_declaration",
        "find_implementations",
        "get_diagnostics_for_file",
        "get_diagnostics_for_symbol",
    }
)


class ConnectorError(RuntimeError):
    """Base class for connector lifecycle failures."""


class ConnectorSessionLost(ConnectorError):
    """The authenticated HTTP transport or MCP session can no longer be used."""


class DaemonIdentityChanged(ConnectorSessionLost):
    """Validated discovery no longer names the daemon used by this session."""


class ConnectorRecoveryError(ConnectorError):
    """A lost session could not be replaced and rebound safely."""


class InvalidDaemonResponse(ConnectorError):
    """A daemon control-plane tool returned an invalid typed envelope."""


@dataclass(frozen=True, slots=True)
class DaemonEndpoint:
    """Validated local discovery and authentication state."""

    daemon_id: str
    url: str
    bearer: BearerSecret
    protocol_version: str
    server_version: str
    build_identity: str = LEGACY_BUILD_IDENTITY

    @classmethod
    def from_runtime_files(cls, metadata: DiscoveryMetadata, bearer: BearerSecret) -> Self:
        return cls(
            daemon_id=metadata.daemon_id,
            url=metadata.endpoint,
            bearer=bearer,
            protocol_version=metadata.protocol_version,
            server_version=metadata.server_version,
            build_identity=metadata.build_identity,
        )


@dataclass(frozen=True, slots=True)
class LeaseGrant:
    """Identity returned by the daemon's ``acquire_lease`` control-plane tool."""

    lease_id: str
    daemon_id: str


class DiscoveryProvider(Protocol):
    """Return already validated discovery and bearer state."""

    async def discover(self) -> DaemonEndpoint: ...


class DaemonSession(Protocol):
    """Narrow daemon integration seam used by the recovery core and tests."""

    async def acquire_lease(self) -> LeaseGrant: ...

    async def heartbeat(self, lease_id: str) -> None: ...

    async def release_lease(self, lease_id: str) -> None: ...

    async def activate_workspace(self, lease_id: str, path: Path) -> types.CallToolResult: ...

    async def list_tools(self) -> types.ListToolsResult: ...

    async def call_tool(
        self,
        lease_id: str,
        name: str,
        arguments: Mapping[str, object] | None,
    ) -> types.CallToolResult: ...

    async def aclose(self) -> None: ...


class SessionFactory(Protocol):
    """Open one authenticated, initialized MCP session to a daemon endpoint."""

    async def connect(self, endpoint: DaemonEndpoint) -> DaemonSession: ...


type EnsureDaemon = Callable[[], Awaitable[None]]


class RuntimeDiscoveryProvider:
    """Read validated runtime files, optionally invoking connect-or-start once."""

    def __init__(
        self,
        *,
        is_process_identity_live: ProcessIdentityValidator,
        runtime_root: Path = RUNTIME_ROOT,
        ensure_daemon: EnsureDaemon | None = None,
    ) -> None:
        self._runtime_root = runtime_root
        self._is_process_identity_live = is_process_identity_live
        self._ensure_daemon = ensure_daemon

    async def discover(self) -> DaemonEndpoint:
        try:
            return self._read()
        except RuntimeFileError:
            if self._ensure_daemon is None:
                raise
        await self._ensure_daemon()
        return self._read()

    def _read(self) -> DaemonEndpoint:
        metadata = read_discovery_metadata(
            self._runtime_root,
            is_process_identity_live=self._is_process_identity_live,
        )
        bearer = read_bearer_secret(self._runtime_root)
        return DaemonEndpoint.from_runtime_files(metadata, bearer)


class McpDaemonSession:
    """Pinned-SDK adapter around an authenticated Streamable HTTP session."""

    def __init__(self, client: ClientSession, stack: AsyncExitStack) -> None:
        self._client = client
        self._stack = stack
        self._closed = False

    async def acquire_lease(self) -> LeaseGrant:
        result = await self._call_control(ACQUIRE_LEASE_TOOL)
        data = _require_ok_data(result, ACQUIRE_LEASE_TOOL)
        lease_id = data.get("lease_id")
        daemon_id = data.get("daemon_id")
        if not isinstance(lease_id, str) or not isinstance(daemon_id, str):
            raise InvalidDaemonResponse("acquire_lease omitted lease_id or daemon_id")
        return LeaseGrant(lease_id=lease_id, daemon_id=daemon_id)

    async def heartbeat(self, lease_id: str) -> None:
        result = await self._call_control(HEARTBEAT_TOOL, {"lease_id": lease_id})
        _require_ok_data(result, HEARTBEAT_TOOL)

    async def release_lease(self, lease_id: str) -> None:
        result = await self._call_control(RELEASE_LEASE_TOOL, {"lease_id": lease_id})
        _require_ok_data(result, RELEASE_LEASE_TOOL)

    async def activate_workspace(self, lease_id: str, path: Path) -> types.CallToolResult:
        result = await self.call_tool(lease_id, ACTIVATE_WORKSPACE_TOOL, {"absolute_path": str(path)})
        _require_ok_data(result, ACTIVATE_WORKSPACE_TOOL)
        return result

    async def list_tools(self) -> types.ListToolsResult:
        try:
            return await self._client.list_tools()
        except Exception as exc:
            raise ConnectorSessionLost("MCP tools/list failed") from exc

    async def call_tool(
        self,
        lease_id: str,
        name: str,
        arguments: Mapping[str, object] | None,
    ) -> types.CallToolResult:
        try:
            return await self._client.call_tool(
                name,
                dict(arguments) if arguments is not None else None,
                meta={"serena_light": {"lease_id": lease_id}},
            )
        except Exception as exc:
            raise ConnectorSessionLost(f"MCP tools/call failed for {name}") from exc

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        # A dead Streamable HTTP task group cancels its owning AnyIO scope.
        # Shield teardown so the scope can be exited before recovery reuses
        # this connector task for the replacement session.
        with anyio.CancelScope(shield=True):
            await self._stack.aclose()

    async def _call_control(
        self,
        name: str,
        arguments: Mapping[str, object] | None = None,
    ) -> types.CallToolResult:
        try:
            return await self._client.call_tool(name, dict(arguments) if arguments is not None else None)
        except Exception as exc:
            raise ConnectorSessionLost(f"daemon control-plane call failed for {name}") from exc


type _SessionAction = Callable[[McpDaemonSession], Awaitable[object]]


@dataclass(slots=True)
class _SessionCommand:
    action: _SessionAction
    result: asyncio.Future[object]


class _OwnedMcpDaemonSession:
    """Keep each SDK/AnyIO cancel scope inside its own long-lived task."""

    def __init__(self, endpoint: DaemonEndpoint, connect_timeout_seconds: float) -> None:
        self._endpoint = endpoint
        self._connect_timeout_seconds = connect_timeout_seconds
        self._commands: asyncio.Queue[_SessionCommand | None] = asyncio.Queue()
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._run(), name=f"serena-light-http:{endpoint.daemon_id}")

    async def start(self) -> _OwnedMcpDaemonSession:
        await self._ready
        return self

    async def acquire_lease(self) -> LeaseGrant:
        return await self._submit(lambda session: session.acquire_lease(), LeaseGrant)

    async def heartbeat(self, lease_id: str) -> None:
        await self._submit(lambda session: session.heartbeat(lease_id), type(None))

    async def release_lease(self, lease_id: str) -> None:
        await self._submit(lambda session: session.release_lease(lease_id), type(None))

    async def activate_workspace(self, lease_id: str, path: Path) -> types.CallToolResult:
        return await self._submit(lambda session: session.activate_workspace(lease_id, path), types.CallToolResult)

    async def list_tools(self) -> types.ListToolsResult:
        return await self._submit(lambda session: session.list_tools(), types.ListToolsResult)

    async def call_tool(
        self, lease_id: str, name: str, arguments: Mapping[str, object] | None
    ) -> types.CallToolResult:
        return await self._submit(lambda session: session.call_tool(lease_id, name, arguments), types.CallToolResult)

    async def aclose(self) -> None:
        if self._task.done():
            with suppress(BaseException):
                await self._task
            return
        await self._commands.put(None)
        with suppress(BaseException):
            await self._task

    async def _submit[T](self, action: _SessionAction, expected: type[T]) -> T:
        if self._task.done():
            raise ConnectorSessionLost("daemon MCP session owner exited")
        result: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        await self._commands.put(_SessionCommand(action, result))
        value = await result
        if not isinstance(value, expected):
            raise InvalidDaemonResponse("daemon MCP session returned an invalid result type")
        return value

    async def _run(self) -> None:
        stack = AsyncExitStack()
        direct: McpDaemonSession | None = None
        workers: set[asyncio.Task[None]] = set()
        try:
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {self._endpoint.bearer.value}"},
                    timeout=httpx.Timeout(self._connect_timeout_seconds, read=None),
                    trust_env=False,
                )
            )
            read_stream, write_stream, _get_session_id = await stack.enter_async_context(
                streamable_http_client(self._endpoint.url, http_client=http_client, terminate_on_close=False)
            )
            client = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            initialized = await client.initialize()
            if str(initialized.protocolVersion) != self._endpoint.protocol_version:
                raise DaemonIdentityChanged("daemon protocol version differs from validated discovery")
            direct = McpDaemonSession(client, stack)
            status = await direct._call_control(GET_DAEMON_STATUS_TOOL)
            health = _require_ok_data(status, GET_DAEMON_STATUS_TOOL)
            expected_health = {
                "daemon_id": self._endpoint.daemon_id,
                "protocol_version": self._endpoint.protocol_version,
                "server_version": self._endpoint.server_version,
                "build_identity": self._endpoint.build_identity,
            }
            mismatched = tuple(
                field for field, value in expected_health.items() if health.get(field) != value
            )
            if mismatched:
                raise DaemonIdentityChanged(
                    f"daemon identity or version differs from validated discovery: {mismatched}"
                )
            self._ready.set_result(None)
            while (command := await self._commands.get()) is not None:
                worker = asyncio.create_task(self._execute(command, direct))
                workers.add(worker)
                worker.add_done_callback(workers.discard)
                command.result.add_done_callback(
                    lambda result, task=worker: task.cancel() if result.cancelled() else None
                )
        except BaseException as exc:
            failure = (
                exc
                if isinstance(exc, ConnectorError)
                else ConnectorSessionLost("daemon MCP session setup failed")
            )
            if not self._ready.done():
                self._ready.set_exception(failure)
        finally:
            for worker in workers:
                worker.cancel()
            if workers:
                with anyio.CancelScope(shield=True):
                    await asyncio.gather(*workers, return_exceptions=True)
            if direct is None:
                with anyio.CancelScope(shield=True):
                    await stack.aclose()
            else:
                await direct.aclose()
            failure = ConnectorSessionLost("daemon MCP session owner exited")
            while not self._commands.empty():
                command = self._commands.get_nowait()
                if command is not None and not command.result.done():
                    command.result.set_exception(failure)

    @staticmethod
    async def _execute(command: _SessionCommand, direct: McpDaemonSession) -> None:
        try:
            value = await command.action(direct)
        except BaseException as exc:
            if not command.result.done():
                failure = (
                    exc if isinstance(exc, ConnectorError) else ConnectorSessionLost("daemon MCP session failed")
                )
                command.result.set_exception(failure)
        else:
            if not command.result.done():
                command.result.set_result(value)


class McpSessionFactory:
    """Create authenticated MCP 1.27.1 Streamable HTTP client sessions."""

    def __init__(self, *, connect_timeout_seconds: float = 30.0) -> None:
        self._connect_timeout_seconds = connect_timeout_seconds

    async def connect(self, endpoint: DaemonEndpoint) -> DaemonSession:
        return await _OwnedMcpDaemonSession(endpoint, self._connect_timeout_seconds).start()


class Connector:
    """Own one client lease and safely proxy MCP tools to the shared daemon."""

    def __init__(
        self,
        discovery: DiscoveryProvider,
        sessions: SessionFactory,
        *,
        startup_cwd: Path | None = None,
        heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        inherited_cwd = Path.cwd() if startup_cwd is None else startup_cwd
        if not inherited_cwd.is_absolute():
            raise ValueError("startup_cwd must be absolute")
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        self._startup_cwd = inherited_cwd.resolve()
        self._discovery = discovery
        self._sessions = sessions
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._session: DaemonSession | None = None
        self._endpoint: DaemonEndpoint | None = None
        self._lease: LeaseGrant | None = None
        self._last_binding: Path | None = None
        self._pending_startup_binding: Path | None = self._startup_cwd
        self._generation = 0
        self._lifecycle_lock = asyncio.Lock()
        self._binding_lock = asyncio.Lock()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._background_failure: BaseException | None = None
        self._closed = False

    @property
    def startup_cwd(self) -> Path:
        return self._startup_cwd

    @property
    def last_validated_binding(self) -> Path | None:
        return self._last_binding

    @property
    def lease_id(self) -> str | None:
        return None if self._lease is None else self._lease.lease_id

    @property
    def background_failure(self) -> BaseException | None:
        return self._background_failure

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def start(self) -> None:
        """Connect and acquire a lease without blocking MCP discovery on workspace work."""

        async with self._lifecycle_lock:
            if self._closed:
                raise ConnectorError("connector is closed")
            if self._session is not None:
                return
            endpoint, session, lease = await self._open_session(None)
            self._install(endpoint, session, lease)
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="serena-light-heartbeat")

    async def list_tools(self) -> types.ListToolsResult:
        result = await self._read_with_recovery(lambda session, _lease: session.list_tools())
        visible = [
            tool for tool in result.tools if tool.name not in CONTROL_PLANE_TOOLS | WITHHELD_TOOLS
        ]
        return result.model_copy(update={"tools": visible})

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, object] | None = None,
    ) -> types.CallToolResult:
        if name in WITHHELD_TOOLS:
            return _temporarily_disabled_result(name)
        if name not in {ACTIVATE_WORKSPACE_TOOL, RELEASE_WORKSPACE_TOOL}:
            await self._ensure_startup_binding()
        retryable = name in READ_ONLY_TOOLS
        uncertain_on_loss = name in EDIT_TOOLS or not retryable

        async def invoke(session: DaemonSession, lease: LeaseGrant) -> types.CallToolResult:
            return await session.call_tool(lease.lease_id, name, arguments)

        result = await self._invoke_with_recovery(
            invoke,
            retryable=retryable,
            uncertain_on_loss=uncertain_on_loss,
            operation=name,
        )
        if name == ACTIVATE_WORKSPACE_TOOL and _is_ok(result):
            path = None if arguments is None else arguments.get("absolute_path")
            if not isinstance(path, str) or not Path(path).is_absolute():
                raise InvalidDaemonResponse("successful activate_workspace did not contain absolute_path")
            self._last_binding = Path(path).resolve()
            self._pending_startup_binding = None
        elif name == RELEASE_WORKSPACE_TOOL and _is_ok(result):
            self._last_binding = None
            self._pending_startup_binding = None
        return result

    async def _ensure_startup_binding(self) -> None:
        """Bind inherited cwd once, immediately before the first workspace-dependent call."""

        async with self._binding_lock:
            binding = self._pending_startup_binding
            if binding is None:
                return

            async def activate(session: DaemonSession, lease: LeaseGrant) -> types.CallToolResult:
                return await session.activate_workspace(lease.lease_id, binding)

            await self._invoke_with_recovery(
                activate,
                retryable=True,
                uncertain_on_loss=False,
                operation="startup activate_workspace",
            )
            self._last_binding = binding
            self._pending_startup_binding = None

    async def aclose(self) -> None:
        """Stop heartbeats, release the connector lease, and close HTTP state."""

        heartbeat = self._heartbeat_task
        self._heartbeat_task = None
        if heartbeat is not None:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            session, lease = self._session, self._lease
            self._session = None
            self._lease = None
            self._endpoint = None
            if session is not None and lease is not None:
                with suppress(Exception, asyncio.CancelledError):
                    await session.release_lease(lease.lease_id)
            if session is not None:
                with suppress(Exception, asyncio.CancelledError):
                    await session.aclose()

    async def _read_with_recovery(self, operation: Callable[[DaemonSession, LeaseGrant], Awaitable[object]]):
        return await self._invoke_with_recovery(
            operation,
            retryable=True,
            uncertain_on_loss=False,
            operation="tools/list",
        )

    async def _invoke_with_recovery(
        self,
        invoke: Callable[[DaemonSession, LeaseGrant], Awaitable[object]],
        *,
        retryable: bool,
        uncertain_on_loss: bool,
        operation: str,
    ):
        await self.start()
        session, lease, generation = self._snapshot()
        loss: ConnectorSessionLost | None = None
        try:
            await self._assert_daemon_identity(generation)
            result = await invoke(session, lease)
            if generation == self._generation:
                return result
            loss = DaemonIdentityChanged("connector session changed while request was in flight")
        except ConnectorSessionLost as exc:
            loss = exc

        if uncertain_on_loss:
            recovery_error = await self._recover_for_uncertain(generation)
            return _uncertain_result(operation, loss, recovery_error)

        if not retryable:
            raise loss

        try:
            await self._recover(generation)
        except BaseException as recovery_error:
            raise ConnectorRecoveryError(f"could not recover interrupted {operation}") from recovery_error

        retry_session, retry_lease, retry_generation = self._snapshot()
        await self._assert_daemon_identity(retry_generation)
        result = await invoke(retry_session, retry_lease)
        if retry_generation != self._generation:
            raise ConnectorSessionLost(f"recovered {operation} lost its replacement session")
        return result

    async def _recover_for_uncertain(self, generation: int) -> BaseException | None:
        try:
            await self._recover(generation)
        except BaseException as exc:
            return exc
        return None

    async def _recover(self, observed_generation: int) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                raise ConnectorError("connector closed during recovery")
            if self._generation != observed_generation:
                return
            binding = self._last_binding
            endpoint, session, lease = await self._open_session(binding)
            old_session, old_lease = self._session, self._lease
            self._install(endpoint, session, lease)
            self._background_failure = None
            if old_session is not None and old_lease is not None:
                with suppress(Exception, asyncio.CancelledError):
                    await old_session.release_lease(old_lease.lease_id)
            if old_session is not None:
                with suppress(Exception, asyncio.CancelledError):
                    await old_session.aclose()

    async def _open_session(self, binding: Path | None) -> tuple[DaemonEndpoint, DaemonSession, LeaseGrant]:
        endpoint = await self._discovery.discover()
        session = await self._sessions.connect(endpoint)
        lease: LeaseGrant | None = None
        try:
            lease = await session.acquire_lease()
            if lease.daemon_id != endpoint.daemon_id:
                raise DaemonIdentityChanged("lease was issued by a different daemon identity")
            if binding is not None:
                if not binding.is_absolute():
                    raise ConnectorError("remembered workspace binding is not absolute")
                await session.activate_workspace(lease.lease_id, binding)
            return endpoint, session, lease
        except BaseException:
            if lease is not None:
                with suppress(Exception, asyncio.CancelledError):
                    await session.release_lease(lease.lease_id)
            with suppress(Exception, asyncio.CancelledError):
                await session.aclose()
            raise

    def _install(self, endpoint: DaemonEndpoint, session: DaemonSession, lease: LeaseGrant) -> None:
        self._endpoint = endpoint
        self._session = session
        self._lease = lease
        self._generation += 1

    def _snapshot(self) -> tuple[DaemonSession, LeaseGrant, int]:
        if self._closed:
            raise ConnectorError("connector is closed")
        if self._session is None or self._lease is None:
            raise ConnectorError("connector has no active daemon session")
        return self._session, self._lease, self._generation

    async def _assert_daemon_identity(self, generation: int) -> None:
        try:
            discovered = await self._discovery.discover()
        except Exception as exc:
            raise ConnectorSessionLost("validated daemon discovery is unavailable") from exc
        endpoint = self._endpoint
        if generation != self._generation or endpoint is None or discovered != endpoint:
            raise DaemonIdentityChanged("validated daemon identity, version, endpoint, or bearer changed")

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval_seconds)
            try:
                session, lease, generation = self._snapshot()
                await self._assert_daemon_identity(generation)
                await session.heartbeat(lease.lease_id)
                if generation != self._generation:
                    continue
            except asyncio.CancelledError:
                raise
            except ConnectorSessionLost:
                try:
                    await self._recover(generation)
                except BaseException as exc:
                    self._background_failure = exc
            except BaseException as exc:
                self._background_failure = exc


def build_proxy_server(connector: Connector, *, name: str = CONNECTOR_NAME, version: str = CONNECTOR_VERSION) -> Server:
    """Build the stdio-facing MCP server with dynamic remote tool discovery."""

    server = Server(name, version=version)

    @server.list_tools()
    async def list_tools() -> types.ListToolsResult:
        return await connector.list_tools()

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, object]) -> types.CallToolResult:
        return await connector.call_tool(name, arguments)

    return server


async def run_stdio_proxy(
    connector: Connector,
    *,
    stdin: anyio.AsyncFile[str] | None = None,
    stdout: anyio.AsyncFile[str] | None = None,
) -> None:
    """Run one connector as an MCP stdio server until its input closes."""

    server = build_proxy_server(connector)
    try:
        await connector.start()
        async with stdio_server(stdin=stdin, stdout=stdout) as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        await connector.aclose()


def _require_ok_data(result: types.CallToolResult, operation: str) -> dict[str, object]:
    payload = result.structuredContent
    if result.isError or not isinstance(payload, dict) or payload.get("ok") is not True:
        raise InvalidDaemonResponse(f"{operation} returned a failed or malformed envelope")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise InvalidDaemonResponse(f"{operation} omitted structured data")
    return data


def _is_ok(result: object) -> bool:
    return (
        isinstance(result, types.CallToolResult)
        and not result.isError
        and isinstance(result.structuredContent, dict)
        and result.structuredContent.get("ok") is True
    )


def _uncertain_result(
    operation: str,
    loss: BaseException,
    recovery_error: BaseException | None,
) -> types.CallToolResult:
    details: dict[str, object] = {
        "operation": operation,
        "requires_current_reread": operation in EDIT_TOOLS,
        "transport_failure": type(loss).__name__,
    }
    if recovery_error is not None:
        details["recovery"] = "failed"
        details["recovery_failure"] = type(recovery_error).__name__
    envelope = error(
        ErrorCode.UNCERTAIN,
        retry=RetryMetadata(retryable=False),
        details=details,
    )
    payload = envelope.to_dict()
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=envelope.to_json())],
        structuredContent=payload,
        isError=True,
    )


def _temporarily_disabled_result(operation: str) -> types.CallToolResult:
    envelope = error(
        ErrorCode.UNSUPPORTED,
        retry=RetryMetadata(retryable=False),
        details={"operation": operation, "reason": "temporarily_disabled_pending_reacceptance"},
    )
    payload = envelope.to_dict()
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=envelope.to_json())],
        structuredContent=payload,
        isError=True,
    )


__all__: Sequence[str] = (
    "ACQUIRE_LEASE_TOOL",
    "ACTIVATE_WORKSPACE_TOOL",
    "CONNECTOR_NAME",
    "CONTROL_PLANE_TOOLS",
    "GET_DAEMON_STATUS_TOOL",
    "HEARTBEAT_INTERVAL_SECONDS",
    "HEARTBEAT_TOOL",
    "RELEASE_LEASE_TOOL",
    "Connector",
    "ConnectorError",
    "ConnectorRecoveryError",
    "ConnectorSessionLost",
    "DaemonEndpoint",
    "DaemonIdentityChanged",
    "DaemonSession",
    "DiscoveryProvider",
    "InvalidDaemonResponse",
    "LeaseGrant",
    "McpDaemonSession",
    "McpSessionFactory",
    "RuntimeDiscoveryProvider",
    "SessionFactory",
    "build_proxy_server",
    "run_stdio_proxy",
)
