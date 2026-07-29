"""Production console wiring for the stdio connector and shared daemon."""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import httpx
import psutil
import uvicorn
from mcp.types import LATEST_PROTOCOL_VERSION

from serena_light import __version__
from serena_light.connector import Connector, McpSessionFactory, RuntimeDiscoveryProvider, run_stdio_proxy
from serena_light.daemon.leases import LeaseLifecycle
from serena_light.daemon.server import (
    HEALTH_PATH,
    LOOPBACK_HOST,
    connect_or_start,
    create_daemon_app,
    spawn_detached_process,
    validate_health_identity,
)
from serena_light.daemon.service import WorkspaceDaemonService
from serena_light.runtime_files import (
    BEARER_NAME,
    DISCOVERY_NAME,
    RUNTIME_ROOT,
    BearerSecret,
    DiscoveryMetadata,
    RuntimeFileError,
    create_bearer_secret,
    prepare_runtime_directory,
    read_bearer_secret,
    read_discovery_metadata,
    write_discovery_metadata,
)
from serena_light.workspace.identity import PinnedMsRoots, WorkspaceKind, WorkspacePolicy
from serena_light.workspace.registry import WorkspaceRuntimeRegistry

DAEMON_STARTUP_TIMEOUT_SECONDS = 30.0
DAEMON_HEALTH_TIMEOUT_SECONDS = 1.0
DAEMON_SWEEP_INTERVAL_SECONDS = 1.0
SERVER_STARTUP_POLL_SECONDS = 0.01


class _Runtime(Protocol):
    def stop(self) -> None: ...


class _SweepService(Protocol):
    async def sweep(self) -> object: ...


class _Server(Protocol):
    started: bool
    should_exit: bool

    async def serve(self, sockets: list[socket.socket] | None = None) -> None: ...


type PhysicalWorkspaceKey = tuple[WorkspaceKind, Path]
type RuntimeBuilder = Callable[[PhysicalWorkspaceKey, WorkspacePolicy], _Runtime]
type AsyncCallback = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _DiscoveredDaemon:
    metadata: DiscoveryMetadata
    bearer: BearerSecret


class _RuntimeOwner:
    """Lazily construct runtimes and retain exactly the instances to stop."""

    def __init__(self, policy: WorkspacePolicy, *, builder: RuntimeBuilder | None = None) -> None:
        self._policy = policy
        self._builder = builder
        self._lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._runtimes: set[_Runtime] = set()

    def create(self, identity: PhysicalWorkspaceKey) -> _Runtime:
        builder = self._builder
        if builder is None:
            # Keep the moving workspace-runtime composition seam out of module import
            # time.  A daemon can perform discovery/health work without importing any
            # language adapter or starting a language server.
            from serena_light.workspace.runtime import WorkspaceRuntime

            runtime = WorkspaceRuntime(identity, path_policy=self._policy)
        else:
            runtime = builder(identity, self._policy)
        with self._lock:
            self._runtimes.add(runtime)
        return runtime

    def stop(self, runtime: _Runtime) -> None:
        # A cancelled sweep cannot cancel its already-running ``to_thread``
        # stop.  Serialize that path with final shutdown to avoid stopping the
        # same runtime concurrently.
        with self._stop_lock:
            with self._lock:
                if runtime not in self._runtimes:
                    return
            try:
                runtime.stop()
            finally:
                with self._lock:
                    self._runtimes.discard(runtime)

    async def stop_all(self) -> None:
        with self._lock:
            runtimes = tuple(self._runtimes)
        if runtimes:
            await asyncio.gather(*(asyncio.to_thread(self.stop, runtime) for runtime in runtimes))


def is_process_identity_live(pid: int, expected_create_time: float) -> bool:
    """Validate both PID liveness and psutil's process creation timestamp."""

    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if isinstance(expected_create_time, bool) or not isinstance(expected_create_time, int | float):
        return False
    try:
        process = psutil.Process(pid)
        return (
            process.is_running()
            and process.status() != psutil.STATUS_ZOMBIE
            and process.create_time() == float(expected_create_time)
        )
    except (psutil.Error, OSError, ValueError):
        return False


def _daemon_argv() -> tuple[str, ...]:
    """Launch the daemon from this installed interpreter without PATH lookup."""

    return (sys.executable, "-I", "-m", "serena_light.cli", "daemon")


def _daemon_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ if environ is None else environ)
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _spawn_daemon() -> object:
    return spawn_detached_process(
        _daemon_argv(),
        cwd=Path("/"),
        env=_daemon_environment(),
    )


def _read_daemon(runtime_root: Path) -> _DiscoveredDaemon:
    metadata = read_discovery_metadata(
        runtime_root,
        is_process_identity_live=is_process_identity_live,
    )
    return _DiscoveredDaemon(metadata=metadata, bearer=read_bearer_secret(runtime_root))


def _health_url(endpoint: str) -> str:
    return endpoint.removesuffix("/mcp") + HEALTH_PATH


def _daemon_is_healthy(candidate: _DiscoveredDaemon) -> bool:
    try:
        response = httpx.get(
            _health_url(candidate.metadata.endpoint),
            headers={"Authorization": f"Bearer {candidate.bearer.value}"},
            timeout=DAEMON_HEALTH_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            return False
        validate_health_identity(candidate.metadata, payload)
    except (httpx.HTTPError, RuntimeError, ValueError):
        return False
    return True


def _ensure_daemon_sync(runtime_root: Path = RUNTIME_ROOT) -> None:
    connect_or_start(
        runtime_root=runtime_root,
        discover=lambda: _read_daemon(runtime_root),
        is_healthy=_daemon_is_healthy,
        spawn=_spawn_daemon,
        timeout_seconds=DAEMON_STARTUP_TIMEOUT_SECONDS,
    )


async def _ensure_daemon(runtime_root: Path = RUNTIME_ROOT) -> None:
    await asyncio.to_thread(_ensure_daemon_sync, runtime_root)


async def _run_connector(runtime_root: Path = RUNTIME_ROOT) -> None:
    ensure = lambda: _ensure_daemon(runtime_root)  # noqa: E731 - protocol callback retains the selected root
    await ensure()
    discovery = RuntimeDiscoveryProvider(
        runtime_root=runtime_root,
        is_process_identity_live=is_process_identity_live,
        ensure_daemon=ensure,
    )
    connector = Connector(discovery, McpSessionFactory(), startup_cwd=Path.cwd())
    await run_stdio_proxy(connector)


def _bind_loopback_socket() -> socket.socket:
    bound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        bound.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        bound.bind((LOOPBACK_HOST, 0))
        bound.set_inheritable(False)
        return bound
    except BaseException:
        bound.close()
        raise


async def _sweep_periodically(service: _SweepService, interval_seconds: float) -> None:
    if interval_seconds <= 0:
        raise ValueError("sweep interval must be positive")
    while True:
        await asyncio.sleep(interval_seconds)
        await service.sweep()


async def _serve_with_lifecycle(
    *,
    server: _Server,
    bound_socket: socket.socket,
    service: _SweepService,
    on_started: AsyncCallback,
    on_stopping: AsyncCallback,
    sweep_interval_seconds: float = DAEMON_SWEEP_INTERVAL_SECONDS,
    startup_poll_seconds: float = SERVER_STARTUP_POLL_SECONDS,
) -> None:
    """Publish only after startup and make sweeping/shutdown independently owned."""

    if startup_poll_seconds <= 0:
        raise ValueError("startup poll interval must be positive")
    server_task = asyncio.create_task(server.serve(sockets=[bound_socket]), name="serena-light-http")
    sweep_task = asyncio.create_task(
        _sweep_periodically(service, sweep_interval_seconds),
        name="serena-light-lease-sweep",
    )
    try:
        while not server.started:
            if server_task.done():
                await server_task
                raise RuntimeError("daemon HTTP server exited before startup")
            if sweep_task.done():
                await sweep_task
                raise RuntimeError("daemon lease sweep exited before startup")
            await asyncio.sleep(startup_poll_seconds)

        await on_started()
        done, _pending = await asyncio.wait(
            (server_task, sweep_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if sweep_task in done:
            server.should_exit = True
            await server_task
            await sweep_task
        await server_task
    finally:
        server.should_exit = True
        if not server_task.done():
            with suppress(asyncio.CancelledError):
                await server_task
        sweep_task.cancel()
        # Preserve the exception already selected by the main lifecycle path;
        # cleanup must not be skipped merely because the completed sweep task
        # raises again when awaited here.
        with suppress(BaseException):
            await sweep_task
        await on_stopping()


def _remove_owned_runtime_artifacts(runtime_root: Path, daemon_id: str) -> None:
    """Remove only discovery and bearer files still naming this daemon."""

    try:
        metadata = read_discovery_metadata(
            runtime_root,
            is_process_identity_live=lambda _pid, _created: True,
        )
    except RuntimeFileError:
        return
    if metadata.daemon_id != daemon_id:
        return
    directory_fd = os.open(runtime_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in (DISCOVERY_NAME, BEARER_NAME):
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _remove_stale_discovery(runtime_root: Path) -> None:
    """Prevent readers from pairing old discovery with a newly written bearer."""

    directory_fd = os.open(runtime_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with suppress(FileNotFoundError):
            os.unlink(DISCOVERY_NAME, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


async def _run_daemon(runtime_root: Path = RUNTIME_ROOT) -> None:
    runtime_root = prepare_runtime_directory(runtime_root)
    _remove_stale_discovery(runtime_root)
    bearer = create_bearer_secret(runtime_root)
    daemon_id = str(uuid4())
    process_start_time = psutil.Process(os.getpid()).create_time()

    policy = WorkspacePolicy(ms_roots=PinnedMsRoots.resolve())
    runtime_owner = _RuntimeOwner(policy)
    registry = WorkspaceRuntimeRegistry(runtime_owner.create)
    lifecycle = LeaseLifecycle(clock=time.monotonic)
    service = WorkspaceDaemonService(
        lifecycle=lifecycle,
        registry=registry,
        resolver=policy,
        runtime_stopper=runtime_owner.stop,
    )

    bound_socket = _bind_loopback_socket()
    port = int(bound_socket.getsockname()[1])
    endpoint = f"http://{LOOPBACK_HOST}:{port}/mcp"
    app = create_daemon_app(service=service, bearer=bearer, daemon_id=daemon_id)
    config = uvicorn.Config(
        app,
        host=LOOPBACK_HOST,
        port=port,
        lifespan="on",
        access_log=False,
        proxy_headers=False,
        server_header=False,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    metadata = DiscoveryMetadata.create(
        daemon_id=daemon_id,
        pid=os.getpid(),
        process_start_time=process_start_time,
        endpoint=endpoint,
        protocol_version=LATEST_PROTOCOL_VERSION,
        server_version=__version__,
    )

    async def publish_discovery() -> None:
        await asyncio.to_thread(write_discovery_metadata, runtime_root, metadata)

    async def cleanup() -> None:
        await asyncio.to_thread(_remove_owned_runtime_artifacts, runtime_root, daemon_id)
        await runtime_owner.stop_all()
        bound_socket.close()

    try:
        await _serve_with_lifecycle(
            server=server,
            bound_socket=bound_socket,
            service=service,
            on_started=publish_discovery,
            on_stopping=cleanup,
        )
    finally:
        if bound_socket.fileno() >= 0:
            bound_socket.close()


def _parse_no_arguments(program: str, argv: Sequence[str] | None) -> None:
    parser = argparse.ArgumentParser(prog=program)
    parser.parse_args(argv)


def connector_main(argv: Sequence[str] | None = None) -> int:
    """Run the per-client stdio MCP connector."""

    _parse_no_arguments("serena-light", argv)
    asyncio.run(_run_connector())
    return 0


def daemon_main(argv: Sequence[str] | None = None) -> int:
    """Run the shared authenticated loopback daemon."""

    _parse_no_arguments("serena-light-daemon", argv)
    asyncio.run(_run_daemon())
    return 0


def _module_main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["daemon"]:
        return daemon_main(arguments[1:])
    if arguments[:1] == ["connector"]:
        return connector_main(arguments[1:])
    return connector_main(arguments)


if __name__ == "__main__":  # pragma: no cover - exercised through the installed process boundary
    raise SystemExit(_module_main())
