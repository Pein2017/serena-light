"""Authenticated loopback Streamable HTTP daemon foundation.

The MCP HTTP session is transport correlation only.  The injected service owns
daemon-issued leases and workspace bindings; this module never creates a
daemon-global active workspace.
"""

from __future__ import annotations

import errno
import fcntl
import hmac
import os
import stat
import subprocess
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID, uuid4
from weakref import WeakKeyDictionary

import psutil
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import LATEST_PROTOCOL_VERSION
from pydantic import StrictBool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from serena_light import __version__
from serena_light.build_identity import validate_build_identity
from serena_light.daemon.leases import LeaseExpiredError as LifecycleLeaseExpiredError
from serena_light.processes import terminate_process_tree_with_kill_fallback
from serena_light.runtime_files import (
    LEGACY_BUILD_IDENTITY,
    PRIVATE_FILE_MODE,
    BearerSecret,
    DiscoveryMetadata,
    RuntimeFileError,
    prepare_runtime_directory,
)
from serena_light.tools.envelopes import ErrorCode, RetryMetadata, error

LOOPBACK_HOST = "127.0.0.1"
HEALTH_PATH = "/health"
MIGRATION_STATUS_PATH = "/migration-status"
MCP_PATH = "/mcp"
STARTUP_LOCK_NAME = "startup.lock"
DEFAULT_STARTUP_TIMEOUT_SECONDS = 10.0
DEFAULT_HEALTH_POLL_SECONDS = 0.05


class DaemonConfigurationError(ValueError):
    """Raised when a daemon could become reachable beyond IPv4 loopback."""


class DaemonIdentityError(RuntimeError):
    """Raised when health does not identify the discovered daemon exactly."""


class DaemonStartupError(RuntimeError):
    """Raised when connect-or-start cannot observe a healthy daemon in time."""


class LeaseExpiredError(RuntimeError):
    """Service-seam signal that a lease no longer authorizes work."""


class DaemonService(Protocol):
    """Lease/workspace seam implemented by the daemon runtime owner.

    Methods return the contents for a successful ``data`` envelope.  They may
    return domain facts in any JSON-serializable mapping; this transport layer
    adds the stable ``ok`` envelope and daemon identity where required.
    """

    async def status(self, *, mcp_session_id: str) -> Mapping[str, object]: ...

    async def acquire_lease(self, *, mcp_session_id: str) -> Mapping[str, object]: ...

    async def heartbeat(self, *, lease_id: str) -> Mapping[str, object]: ...

    async def release_lease(self, *, lease_id: str, immediate: bool) -> Mapping[str, object]: ...

    async def activate_workspace(self, *, lease_id: str, absolute_path: str) -> Mapping[str, object]: ...

    async def release_workspace(
        self, *, lease_id: str, immediate: bool = False
    ) -> Mapping[str, object]: ...

    async def get_runtime_status(self, *, lease_id: str) -> Mapping[str, object]: ...

    async def semantic_operation(
        self, *, lease_id: str, operation: str, **kwargs: object
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class DaemonHealth:
    daemon_id: str
    server_version: str = __version__
    protocol_version: str = LATEST_PROTOCOL_VERSION
    build_identity: str = LEGACY_BUILD_IDENTITY

    def __post_init__(self) -> None:
        if not isinstance(self.daemon_id, str):
            raise DaemonConfigurationError("daemon_id must be a UUID")
        try:
            UUID(self.daemon_id)
        except (AttributeError, ValueError) as exc:
            raise DaemonConfigurationError("daemon_id must be a UUID") from exc
        if (
            not isinstance(self.server_version, str)
            or not isinstance(self.protocol_version, str)
            or not self.server_version
            or not self.protocol_version
        ):
            raise DaemonConfigurationError("daemon versions must be non-empty")
        try:
            validate_build_identity(self.build_identity)
        except ValueError as exc:
            raise DaemonConfigurationError("daemon build identity must be a SHA-256 digest") from exc

    def as_data(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DetachedProcess:
    """Minimal observation returned without retaining a connector-owned handle."""

    pid: int
    process: subprocess.Popen[bytes] = field(repr=False, compare=False)


class _BearerAuthentication:
    """Authenticate every HTTP route before Starlette or MCP sees the request."""

    def __init__(self, app: ASGIApp, secret: BearerSecret) -> None:
        if not secret.value:
            raise DaemonConfigurationError("bearer secret must be non-empty")
        self._app = app
        self._expected = f"Bearer {secret.value}"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        authorization = _header_value(scope, b"authorization")
        if authorization is None or not hmac.compare_digest(authorization, self._expected):
            response = JSONResponse(
                {"ok": False, "error": {"code": "UNAUTHORIZED", "message": "invalid bearer token"}},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


class _MCPSessionIds:
    """Assign opaque transport correlation IDs without making them leases."""

    def __init__(self) -> None:
        self._ids: WeakKeyDictionary[Any, str] = WeakKeyDictionary()

    def for_context(self, context: Context) -> str:
        session = context.session
        correlation = self._ids.get(session)
        if correlation is None:
            correlation = str(uuid4())
            self._ids[session] = correlation
        return correlation


def create_daemon_app(
    *,
    service: DaemonService,
    bearer: BearerSecret,
    daemon_id: str,
    host: str = LOOPBACK_HOST,
    server_version: str = __version__,
    protocol_version: str = LATEST_PROTOCOL_VERSION,
    build_identity: str = LEGACY_BUILD_IDENTITY,
) -> ASGIApp:
    """Build one stateful MCP 1.27.1 Streamable HTTP ASGI application.

    FastMCP owns standards-compliant initialize/session/list-tools/call-tool
    handling.  The outer bearer gate deliberately wraps the complete Starlette
    application, including health, so no MCP or service callback can run first.
    """

    validate_loopback_host(host)
    health = DaemonHealth(
        daemon_id=daemon_id,
        server_version=server_version,
        protocol_version=protocol_version,
        build_identity=build_identity,
    )
    mcp = FastMCP(
        name="serena-light",
        host=host,
        streamable_http_path=MCP_PATH,
        json_response=True,
        stateless_http=False,
    )
    session_ids = _MCPSessionIds()

    @mcp.custom_route(HEALTH_PATH, methods=["GET"], include_in_schema=False)
    async def health_route(_request: Request) -> JSONResponse:
        return JSONResponse(_success(health.as_data()))

    @mcp.custom_route(MIGRATION_STATUS_PATH, methods=["GET"], include_in_schema=False)
    async def migration_status_route(_request: Request) -> JSONResponse:
        callback = cast(
            Callable[[], Awaitable[Mapping[str, object]]],
            cast(Any, service).migration_status,
        )
        lifetime = dict(await callback())
        identity: dict[str, object] = {
            "daemon_id": health.daemon_id,
            "pid": os.getpid(),
            "process_start_time": psutil.Process(os.getpid()).create_time(),
            "build_identity": health.build_identity,
        }
        identity.update(lifetime)
        return JSONResponse(_success(identity))

    @mcp.tool(name="get_daemon_status", structured_output=True)
    async def get_daemon_status(context: Context) -> dict[str, object]:
        data = dict(await service.status(mcp_session_id=session_ids.for_context(context)))
        return _success(_with_daemon_health(data, health))

    @mcp.tool(name="acquire_lease", structured_output=True)
    async def acquire_lease(context: Context) -> dict[str, object]:
        data = dict(await service.acquire_lease(mcp_session_id=session_ids.for_context(context)))
        return _success(_with_daemon_health(data, health, versions=False))

    @mcp.tool(name="heartbeat", structured_output=True)
    async def heartbeat(lease_id: str) -> dict[str, object]:
        try:
            validated = _validated_uuid(lease_id, "lease_id")
        except ValueError:
            return _lease_expired()
        try:
            return _success(dict(await service.heartbeat(lease_id=validated)))
        except (LeaseExpiredError, LifecycleLeaseExpiredError):
            return _lease_expired()

    @mcp.tool(name="release_lease", structured_output=True)
    async def release_lease(lease_id: str, immediate: bool = False) -> dict[str, object]:
        try:
            validated = _validated_uuid(lease_id, "lease_id")
        except ValueError:
            return _lease_expired()
        try:
            return _success(
                dict(
                    await service.release_lease(
                        lease_id=validated,
                        immediate=immediate,
                    )
                )
            )
        except (LeaseExpiredError, LifecycleLeaseExpiredError):
            return _lease_expired()

    @mcp.tool(name="activate_workspace", structured_output=True)
    async def activate_workspace(absolute_path: str, context: Context) -> dict[str, object]:
        try:
            lease_id = lease_id_from_context(context)
        except ValueError:
            return _lease_expired()
        try:
            return _as_tool_envelope(
                await service.activate_workspace(lease_id=lease_id, absolute_path=absolute_path)
            )
        except (LeaseExpiredError, LifecycleLeaseExpiredError):
            return _lease_expired()

    async def bound_call(context: Context, operation: str, **kwargs: object) -> dict[str, object]:
        """Invoke one public bound operation with lease authority from request metadata."""

        try:
            lease_id = lease_id_from_context(context)
        except ValueError:
            return _lease_expired()
        try:
            if operation in {"release_workspace", "get_runtime_status"}:
                callback = cast(Callable[..., Any], getattr(service, operation))
                return _as_tool_envelope(await callback(lease_id=lease_id))
            return _as_tool_envelope(
                await service.semantic_operation(lease_id=lease_id, operation=operation, **kwargs)
            )
        except (LeaseExpiredError, LifecycleLeaseExpiredError):
            return _lease_expired()

    @mcp.tool(name="release_workspace", structured_output=True)
    async def release_workspace(context: Context, immediate: StrictBool = False) -> dict[str, object]:
        try:
            lease_id = lease_id_from_context(context)
        except ValueError:
            return _lease_expired()
        try:
            return _as_tool_envelope(
                await service.release_workspace(lease_id=lease_id, immediate=immediate)
            )
        except (LeaseExpiredError, LifecycleLeaseExpiredError):
            return _lease_expired()

    @mcp.tool(name="get_runtime_status", structured_output=True)
    async def get_runtime_status(context: Context) -> dict[str, object]:
        result = await bound_call(context, "get_runtime_status")
        if result.get("ok") is not True:
            return result
        data = result.get("data")
        assert isinstance(data, Mapping)
        return _success(_with_daemon_health({str(key): value for key, value in data.items()}, health))

    @mcp.tool(name="get_symbols_overview", structured_output=True)
    async def get_symbols_overview(
        relative_path: str,
        context: Context,
        max_depth: int = 1,
        max_answer_chars: int = 12_000,
    ) -> dict[str, object]:
        return await bound_call(
            context,
            "get_symbols_overview",
            relative_path=relative_path,
            max_depth=max_depth,
            max_answer_chars=max_answer_chars,
        )

    @mcp.tool(name="find_symbol", structured_output=True)
    async def find_symbol(
        name_path: str,
        context: Context,
        relative_path: str | None = None,
        substring_matching: bool = False,
        include_body: bool = False,
        include_info: bool = False,
        max_answer_chars: int = 12_000,
    ) -> dict[str, object]:
        return await bound_call(
            context,
            "find_symbol",
            name_path=name_path,
            relative_path=relative_path,
            substring_matching=substring_matching,
            include_body=include_body,
            include_info=include_info,
            max_answer_chars=max_answer_chars,
        )

    @mcp.tool(name="find_declaration", structured_output=True)
    async def find_declaration(
        relative_path: str,
        regex: str,
        context: Context,
        containing_symbol_name_path: str | None = None,
        include_body: bool = False,
        include_info: bool = False,
    ) -> dict[str, object]:
        return await bound_call(
            context,
            "find_declaration",
            relative_path=relative_path,
            regex=regex,
            containing_symbol_name_path=containing_symbol_name_path,
            include_body=include_body,
            include_info=include_info,
        )

    @mcp.tool(name="find_implementations", structured_output=True)
    async def find_implementations(
        name_path: str,
        relative_path: str,
        context: Context,
        include_info: bool = False,
        include_kinds: list[int] | None = None,
        exclude_kinds: list[int] | None = None,
        max_answer_chars: int = 12_000,
    ) -> dict[str, object]:
        return await bound_call(
            context,
            "find_implementations",
            name_path=name_path,
            relative_path=relative_path,
            include_info=include_info,
            include_kinds=include_kinds,
            exclude_kinds=exclude_kinds,
            max_answer_chars=max_answer_chars,
        )

    @mcp.tool(name="find_referencing_symbols", structured_output=True)
    async def find_referencing_symbols(
        name_path: str,
        relative_path: str,
        context: Context,
        max_snippet_chars: int = 240,
        max_answer_chars: int = 12_000,
    ) -> dict[str, object]:
        return await bound_call(
            context,
            "find_referencing_symbols",
            name_path=name_path,
            relative_path=relative_path,
            max_snippet_chars=max_snippet_chars,
            max_answer_chars=max_answer_chars,
        )

    @mcp.tool(name="get_diagnostics_for_file", structured_output=True)
    async def get_diagnostics_for_file(
        relative_path: str,
        context: Context,
        timeout_seconds: float = 1.0,
        maximum_severity: int = 2,
        max_answer_chars: int = 12_000,
    ) -> dict[str, object]:
        return await bound_call(
            context,
            "get_diagnostics_for_file",
            relative_path=relative_path,
            timeout_seconds=timeout_seconds,
            maximum_severity=maximum_severity,
            max_answer_chars=max_answer_chars,
        )

    @mcp.tool(name="get_diagnostics_for_symbol", structured_output=True)
    async def get_diagnostics_for_symbol(
        relative_path: str,
        name_path: str,
        context: Context,
        timeout_seconds: float = 1.0,
        maximum_severity: int = 2,
        max_answer_chars: int = 12_000,
    ) -> dict[str, object]:
        return await bound_call(
            context,
            "get_diagnostics_for_symbol",
            relative_path=relative_path,
            name_path=name_path,
            timeout_seconds=timeout_seconds,
            maximum_severity=maximum_severity,
            max_answer_chars=max_answer_chars,
        )

    @mcp.tool(name="replace_symbol_body", structured_output=True)
    async def replace_symbol_body(
        name_path: str,
        relative_path: str,
        body: str,
        expected_hash: str,
        context: Context,
    ) -> dict[str, object]:
        return await bound_call(
            context,
            "replace_symbol_body",
            name_path=name_path,
            relative_path=relative_path,
            body=body,
            expected_hash=expected_hash,
        )

    return _BearerAuthentication(mcp.streamable_http_app(), bearer)


def lease_id_from_context(context: Context) -> str:
    """Read connector lease authority from ``_meta.serena_light.lease_id``."""

    meta = context.request_context.meta
    extra = meta.model_extra if meta is not None else None
    namespace = extra.get("serena_light") if extra else None
    if not isinstance(namespace, Mapping):
        raise ValueError("missing _meta.serena_light.lease_id")
    lease_id = namespace.get("lease_id")
    if not isinstance(lease_id, str):
        raise ValueError("missing _meta.serena_light.lease_id")
    return _validated_uuid(lease_id, "_meta.serena_light.lease_id")


def validate_loopback_host(host: str) -> None:
    """Fail closed instead of accepting wildcard, hostname, or IPv6 binds."""

    if host != LOOPBACK_HOST:
        raise DaemonConfigurationError(f"daemon host must be exactly {LOOPBACK_HOST}")


def validate_health_identity(metadata: DiscoveryMetadata, payload: Mapping[str, object]) -> DaemonHealth:
    """Validate health against discovery before a connector reuses a process."""

    data = payload.get("data")
    if payload.get("ok") is not True or not isinstance(data, Mapping):
        raise DaemonIdentityError("daemon health envelope is malformed")
    health_data = cast(Mapping[str, object], data)
    daemon_id = health_data.get("daemon_id")
    server_version = health_data.get("server_version")
    protocol_version = health_data.get("protocol_version")
    build_identity = health_data.get("build_identity")
    if not all(isinstance(value, str) for value in (daemon_id, server_version, protocol_version, build_identity)):
        raise DaemonIdentityError("daemon health identity is malformed")
    assert isinstance(daemon_id, str)
    assert isinstance(server_version, str)
    assert isinstance(protocol_version, str)
    assert isinstance(build_identity, str)
    try:
        health = DaemonHealth(
            daemon_id=daemon_id,
            server_version=server_version,
            protocol_version=protocol_version,
            build_identity=build_identity,
        )
    except DaemonConfigurationError as exc:
        raise DaemonIdentityError("daemon health identity is malformed") from exc
    expected = DaemonHealth(
        daemon_id=metadata.daemon_id,
        server_version=metadata.server_version,
        protocol_version=metadata.protocol_version,
        build_identity=metadata.build_identity,
    )
    if health != expected:
        raise DaemonIdentityError("daemon health identity/version does not match discovery")
    return health


@contextmanager
def exclusive_startup_lock(
    runtime_root: Path,
    *,
    timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_HEALTH_POLL_SECONDS,
) -> Iterator[None]:
    """Acquire the connector startup lock below an already-secure runtime root."""

    if timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("startup lock timeout and poll interval must be positive")
    prepare_runtime_directory(runtime_root)
    directory_fd = os.open(runtime_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    lock_fd: int | None = None
    try:
        lock_fd = os.open(
            STARTUP_LOCK_NAME,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            PRIVATE_FILE_MODE,
            dir_fd=directory_fd,
        )
        info = os.fstat(lock_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeFileError("startup lock has unsafe type or owner")
        if stat.S_IMODE(info.st_mode) != PRIVATE_FILE_MODE:
            os.fchmod(lock_fd, PRIVATE_FILE_MODE)
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DaemonStartupError("timed out waiting for daemon startup lock") from exc
                time.sleep(min(poll_seconds, remaining))
        try:
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(directory_fd)


def connect_or_start[CandidateT](
    *,
    runtime_root: Path,
    discover: Callable[[], CandidateT],
    is_healthy: Callable[[CandidateT], bool],
    spawn: Callable[[], object],
    cleanup_failed_spawn: Callable[[object], None] | None = None,
    timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_HEALTH_POLL_SECONDS,
) -> CandidateT:
    """Serialize discovery/start and return only an identity-validated candidate."""

    if timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("startup timeout and poll interval must be positive")
    deadline = time.monotonic() + timeout_seconds
    with exclusive_startup_lock(
        runtime_root,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
    ):
        candidate = _healthy_candidate(discover, is_healthy)
        if candidate is not None:
            return candidate
        spawned = spawn()
        try:
            while True:
                candidate = _healthy_candidate(discover, is_healthy)
                if candidate is not None:
                    return candidate
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DaemonStartupError("started daemon did not become healthy before timeout")
                time.sleep(min(poll_seconds, remaining))
        except BaseException:
            if cleanup_failed_spawn is not None:
                cleanup_failed_spawn(spawned)
            raise


def spawn_detached_process(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> DetachedProcess:
    """Start a daemon in a new session with no inherited stdio or open FDs."""

    if not argv or any(not isinstance(argument, str) or not argument for argument in argv):
        raise ValueError("detached daemon argv must contain non-empty strings")
    process = subprocess.Popen(
        tuple(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=os.fspath(cwd) if cwd is not None else "/",
        env=dict(env) if env is not None else None,
        close_fds=True,
        start_new_session=True,
    )
    return DetachedProcess(pid=process.pid, process=process)


def cleanup_failed_detached_process(spawned: object) -> None:
    """Reclaim only the process handle created by one failed startup attempt."""

    if not isinstance(spawned, DetachedProcess):
        raise TypeError("failed daemon spawn did not return DetachedProcess")
    terminate_process_tree_with_kill_fallback(
        spawned.process,
        terminate_timeout=2.0,
        kill_timeout=2.0,
        process_name="Serena Light daemon startup",
    )


def _healthy_candidate[CandidateT](
    discover: Callable[[], CandidateT],
    is_healthy: Callable[[CandidateT], bool],
) -> CandidateT | None:
    try:
        candidate = discover()
    except (FileNotFoundError, RuntimeFileError):
        return None
    try:
        return candidate if is_healthy(candidate) else None
    except (ConnectionError, OSError, TimeoutError):
        return None


def _with_daemon_health(
    data: dict[str, object],
    health: DaemonHealth,
    *,
    versions: bool = True,
) -> dict[str, object]:
    data["daemon_id"] = health.daemon_id
    data["build_identity"] = health.build_identity
    if versions:
        data["server_version"] = health.server_version
        data["protocol_version"] = health.protocol_version
    return data


def _success(data: Mapping[str, object]) -> dict[str, object]:
    return {"ok": True, "data": dict(data)}


def _as_tool_envelope(value: Mapping[str, object]) -> dict[str, object]:
    """Preserve runtime envelopes and wrap only plain successful service data."""

    data = dict(value)
    return data if isinstance(data.get("ok"), bool) else _success(data)


def _lease_expired() -> dict[str, object]:
    return error(
        ErrorCode.LEASE_EXPIRED,
        retry=RetryMetadata(retryable=False),
    ).to_dict()


def _validated_uuid(value: str, name: str) -> str:
    try:
        return str(UUID(value))
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{name} must be a UUID") from exc


def _header_value(scope: Scope, name: bytes) -> str | None:
    found: str | None = None
    for raw_name, raw_value in scope.get("headers", ()):
        if raw_name.lower() == name:
            if found is not None:
                return None
            try:
                found = raw_value.decode("latin-1")
            except UnicodeDecodeError:  # pragma: no cover - ASGI headers are latin-1 bytes
                return None
    return found
