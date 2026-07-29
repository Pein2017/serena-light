from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from serena_light import __version__, cli
from serena_light.daemon.leases import LeaseLifecycle
from serena_light.daemon.service import WorkspaceDaemonService
from serena_light.runtime_files import (
    BEARER_NAME,
    DISCOVERY_NAME,
    BearerSecret,
    DiscoveryMetadata,
    RuntimeFileError,
    StartupNonce,
    create_bearer_secret,
    prepare_runtime_layout,
    write_discovery_metadata,
)
from serena_light.workspace.identity import WorkspaceKind, WorkspacePolicy
from serena_light.workspace.registry import ResolvedWorkspace, WorkspaceRuntimeRegistry


def _metadata(*, daemon_id: str | None = None) -> DiscoveryMetadata:
    return DiscoveryMetadata.create(
        daemon_id=daemon_id or str(uuid4()),
        pid=os.getpid(),
        process_start_time=10.0,
        endpoint="http://127.0.0.1:43123/mcp",
        protocol_version="2025-11-25",
        server_version=__version__,
    )


def test_acceptance_overrides_are_complete_isolated_and_source_derived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RuntimeFileError, match="complete set"):
        cli._acceptance_overrides({cli.ACCEPTANCE_RUNTIME_ROOT_ENV: str(tmp_path)})

    environment = {
        cli.PYTEST_CURRENT_TEST_ENV: "isolated acceptance",
        cli.ACCEPTANCE_RUNTIME_ROOT_ENV: str(tmp_path / "runtime"),
        cli.ACCEPTANCE_BUILD_VARIANT_ENV: "old",
        cli.ACCEPTANCE_WARM_GRACE_SECONDS_ENV: "1.5",
        cli.ACCEPTANCE_IDLE_EXIT_SECONDS_ENV: "0.25",
    }
    acceptance = cli._acceptance_overrides(environment)
    assert acceptance is not None
    monkeypatch.setattr(cli, "compute_build_identity", lambda: "a" * 64)
    first = cli._acceptance_build_identity(acceptance)

    changed_variant = cli._acceptance_overrides({**environment, cli.ACCEPTANCE_BUILD_VARIANT_ENV: "new"})
    assert changed_variant is not None
    assert cli._acceptance_build_identity(changed_variant) != first

    monkeypatch.setattr(cli, "compute_build_identity", lambda: "b" * 64)
    assert cli._acceptance_build_identity(acceptance) != first

    with pytest.raises(RuntimeFileError, match="must not overlap"):
        cli._acceptance_overrides({**environment, cli.ACCEPTANCE_RUNTIME_ROOT_ENV: str(cli.RUNTIME_ROOT)})


def test_daemon_command_uses_current_interpreter_module_and_sanitized_environment(tmp_path: Path) -> None:
    assert cli._daemon_argv() == (
        sys.executable,
        "-I",
        "-m",
        "serena_light.cli",
        "daemon",
    )
    ambient = {
        "PATH": "/ambient/bin",
        "PYTHONHOME": "/root/python",
        "PYTHONPATH": "/root/site",
        "HTTP_PROXY": "http://proxy.invalid:8080",
        "Https_PrOxY": "http://proxy.invalid:8080",
        "ALL_PROXY": "socks5://proxy.invalid:1080",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "LANG": "C.UTF-8",
        "KEEP": "yes",
    }
    environment = cli._daemon_environment(
        ambient,
        build_identity="a" * 64,
        build_root=tmp_path / "builds" / ("a" * 64),
        startup_nonce=StartupNonce("nonce"),
        service_home=tmp_path / "home",
    )
    assert environment == {
        "LANG": "C.UTF-8",
        "HOME": str(tmp_path / "home"),
        "GIT_CONFIG_GLOBAL": str(tmp_path / "home" / "gitconfig"),
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), "/usr/bin", "/bin")),
        "PYTHONNOUSERSITE": "1",
        cli.BUILD_IDENTITY_ENV: "a" * 64,
        cli.BUILD_ROOT_ENV: str(tmp_path / "builds" / ("a" * 64)),
        cli.STARTUP_NONCE_ENV: "nonce",
    }
    assert ambient["HTTP_PROXY"] == "http://proxy.invalid:8080"
    assert ambient["NO_PROXY"] == "127.0.0.1,localhost"


def test_detached_daemon_spawn_receives_explicit_safe_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_spawn(
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> object:
        captured.update(argv=argv, cwd=cwd, env=env)
        return object()

    monkeypatch.setattr(cli, "spawn_detached_process", fake_spawn)
    runtime_root = tmp_path / "runtime" / "serena-light"
    runtime_root.parent.mkdir(parents=True)
    layout = prepare_runtime_layout(runtime_root, "a" * 64)
    monkeypatch.setattr(cli, "RUNTIME_ROOT", runtime_root)
    written_git_configs: list[Path] = []
    monkeypatch.setattr(cli, "write_service_git_config", written_git_configs.append)
    monkeypatch.setattr(
        cli,
        "_daemon_environment",
        lambda **_kwargs: {"PYTHONNOUSERSITE": "1"},
    )

    cli._spawn_daemon(layout.build_root)

    assert written_git_configs == [layout.home_root]
    assert captured == {
        "argv": cli._daemon_argv(),
        "cwd": Path("/"),
        "env": {"PYTHONNOUSERSITE": "1"},
    }


def test_pid_validation_requires_live_non_zombie_process_and_exact_create_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        def __init__(self, pid: int) -> None:
            assert pid == 43123

        def is_running(self) -> bool:
            return True

        def status(self) -> str:
            return "sleeping"

        def create_time(self) -> float:
            return 1234.5

    monkeypatch.setattr(cli.psutil, "Process", Process)

    assert cli.is_process_identity_live(43123, 1234.5)
    assert not cli.is_process_identity_live(43123, 1234.6)
    assert not cli.is_process_identity_live(0, 1234.5)


def test_pid_validation_fails_closed_on_psutil_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_pid: int) -> object:
        raise cli.psutil.NoSuchProcess(43123)

    monkeypatch.setattr(cli.psutil, "Process", missing)
    assert not cli.is_process_identity_live(43123, 1234.5)


def test_health_check_uses_bearer_and_validates_discovery_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata = _metadata()
    candidate = cli._DiscoveredDaemon(metadata, BearerSecret("a" * 48))
    captured: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "ok": True,
                "data": {
                    "daemon_id": metadata.daemon_id,
                    "protocol_version": metadata.protocol_version,
                    "server_version": metadata.server_version,
                    "build_identity": metadata.build_identity,
                },
            }

    def fake_get(url: str, **kwargs: object) -> Response:
        captured.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(cli.httpx, "get", fake_get)

    assert cli._daemon_is_healthy(candidate)
    assert captured["url"] == "http://127.0.0.1:43123/health"
    assert captured["headers"] == {"Authorization": "Bearer " + "a" * 48}
    assert captured["trust_env"] is False


def test_legacy_status_fetch_is_authenticated_exact_and_proxy_free(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata = _metadata()
    captured: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "ok": True,
                "data": {
                    "daemon_id": metadata.daemon_id,
                    "pid": metadata.pid,
                    "process_start_time": metadata.process_start_time,
                    "build_identity": metadata.build_identity,
                    "active_holders": 0,
                },
            }

    def fake_get(url: str, **kwargs: object) -> Response:
        captured.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(cli, "read_bearer_secret", lambda _root: BearerSecret("a" * 48))
    monkeypatch.setattr(cli.httpx, "get", fake_get)

    status = cli._fetch_legacy_status(metadata)

    assert status.daemon_id == metadata.daemon_id
    assert status.active_holders == 0
    assert captured["url"] == "http://127.0.0.1:43123/migration-status"
    assert captured["headers"] == {"Authorization": "Bearer " + "a" * 48}
    assert captured["trust_env"] is False


def test_old_daemon_cleanup_never_removes_successor_discovery(tmp_path: Path) -> None:
    build_root = prepare_runtime_layout(tmp_path / "runtime", "a" * 64).build_root
    create_bearer_secret(build_root)
    old = _metadata(daemon_id=str(uuid4()))
    successor = _metadata(daemon_id=str(uuid4()))
    write_discovery_metadata(build_root, old)
    write_discovery_metadata(build_root, successor)

    cli._remove_owned_runtime_artifacts(build_root, old.daemon_id)

    assert (build_root / DISCOVERY_NAME).exists()
    assert (build_root / BEARER_NAME).exists()
    assert json.loads((build_root / DISCOVERY_NAME).read_text())["daemon_id"] == successor.daemon_id

    cli._remove_owned_runtime_artifacts(build_root, successor.daemon_id)
    assert not (build_root / DISCOVERY_NAME).exists()
    assert not (build_root / BEARER_NAME).exists()


def test_health_check_reaches_loopback_with_poisoned_proxy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    metadata = _metadata()

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            assert self.path == "/health"
            payload = {
                "ok": True,
                "data": {
                    "daemon_id": metadata.daemon_id,
                    "protocol_version": metadata.protocol_version,
                    "server_version": metadata.server_version,
                    "build_identity": metadata.build_identity,
                },
            }
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: Any) -> None:
            del format, args
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    candidate = cli._DiscoveredDaemon(
        DiscoveryMetadata.create(
            daemon_id=metadata.daemon_id,
            pid=metadata.pid,
            process_start_time=metadata.process_start_time,
            endpoint=f"http://127.0.0.1:{server.server_port}/mcp",
            protocol_version=metadata.protocol_version,
            server_version=metadata.server_version,
        ),
        BearerSecret("a" * 48),
    )
    try:
        assert cli._daemon_is_healthy(candidate)
    finally:
        server.shutdown()
        thread.join(timeout=1.0)
        server.server_close()


def test_console_entry_points_run_their_async_composition_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    async def connector() -> None:
        called.append("connector")

    async def daemon() -> None:
        called.append("daemon")

    monkeypatch.setattr(cli, "_run_connector", connector)
    monkeypatch.setattr(cli, "_run_daemon", daemon)

    assert cli.connector_main([]) == 0
    assert cli.daemon_main([]) == 0
    assert called == ["connector", "daemon"]


def test_connector_wires_validated_discovery_inherited_cwd_and_stdio_proxy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []

    async def ensure(runtime_root: Path) -> None:
        events.append(("ensure", runtime_root))

    class Discovery:
        def __init__(self, **kwargs: object) -> None:
            events.append(("discovery", kwargs))

    class Sessions:
        pass

    class Connector:
        def __init__(self, discovery: object, sessions: object, *, startup_cwd: Path) -> None:
            events.append(("connector", type(discovery), type(sessions), startup_cwd))

    async def proxy(connector: object) -> None:
        events.append(("proxy", type(connector)))

    monkeypatch.setattr(cli, "_ensure_daemon", ensure)
    monkeypatch.setattr(cli, "RuntimeDiscoveryProvider", Discovery)
    monkeypatch.setattr(cli, "McpSessionFactory", Sessions)
    monkeypatch.setattr(cli, "Connector", Connector)
    monkeypatch.setattr(cli, "run_stdio_proxy", proxy)

    asyncio.run(cli._run_connector(tmp_path))

    assert events[0] == ("ensure", tmp_path)
    assert events[2] == ("connector", Discovery, Sessions, Path.cwd())
    assert events[3] == ("proxy", Connector)


class _FakeServer:
    def __init__(self) -> None:
        self.started = False
        self.should_exit = False

    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        assert sockets is not None and len(sockets) == 1
        self.started = True
        while not self.should_exit:
            await asyncio.sleep(0)


def test_server_start_callback_precedes_periodic_sweep_and_graceful_stop() -> None:
    server = _FakeServer()
    events: list[str] = []

    class Service:
        calls = 0

        async def sweep(self) -> None:
            assert events == ["started"]
            self.calls += 1
            if self.calls == 3:
                server.should_exit = True

    service = Service()

    async def started() -> None:
        assert server.started
        events.append("started")

    async def stopping() -> None:
        events.append("stopped")

    async def exercise() -> None:
        bound = socket.socket()
        try:
            await cli._serve_with_lifecycle(
                server=server,
                bound_socket=bound,
                service=service,
                on_started=started,
                on_stopping=stopping,
                sweep_interval_seconds=0.001,
                startup_poll_seconds=0.001,
            )
        finally:
            bound.close()

    asyncio.run(exercise())

    assert service.calls == 3
    assert events == ["started", "stopped"]


def test_idle_build_daemon_exits_after_all_lifetime_owners_are_gone() -> None:
    server = _FakeServer()
    events: list[str] = []

    class Service:
        calls = 0

        async def sweep(self) -> None:
            self.calls += 1

        def daemon_idle(self) -> bool:
            return True

    service = Service()

    async def started() -> None:
        events.append("started")

    async def stopped() -> None:
        events.append("stopped")

    async def exercise() -> None:
        bound = socket.socket()
        try:
            await cli._serve_with_lifecycle(
                server=server,
                bound_socket=bound,
                service=service,
                on_started=started,
                on_stopping=stopped,
                sweep_interval_seconds=0.001,
                startup_poll_seconds=0.001,
                idle_exit_seconds=0.002,
            )
        finally:
            bound.close()

    asyncio.run(exercise())

    assert service.calls >= 2
    assert server.should_exit
    assert events == ["started", "stopped"]


def test_sweep_failure_requests_server_exit_and_still_runs_cleanup() -> None:
    server = _FakeServer()
    events: list[str] = []

    class Service:
        async def sweep(self) -> None:
            raise RuntimeError("sweep failed")

    async def started() -> None:
        events.append("started")

    async def stopping() -> None:
        events.append("stopped")

    async def exercise() -> None:
        bound = socket.socket()
        try:
            await cli._serve_with_lifecycle(
                server=server,
                bound_socket=bound,
                service=Service(),
                on_started=started,
                on_stopping=stopping,
                sweep_interval_seconds=0.001,
                startup_poll_seconds=0.001,
            )
        finally:
            bound.close()

    with pytest.raises(RuntimeError, match="sweep failed"):
        asyncio.run(exercise())

    assert server.should_exit
    assert events == ["started", "stopped"]


@dataclass(eq=False, slots=True)
class _FailOnceRuntime:
    """A detached runtime whose first stop is rejected, then succeeds."""

    stop_calls: int = 0

    def stop(self) -> None:
        self.stop_calls += 1
        if self.stop_calls == 1:
            raise RuntimeError("stop rejected")


def _single_root_resolution(path: Path) -> ResolvedWorkspace[cli.PhysicalWorkspaceKey]:
    return ResolvedWorkspace(identity=(WorkspaceKind.GIT, path), working_subdirectory=path)


def test_runtime_owner_stop_retains_ownership_until_stop_succeeds() -> None:
    policy = cast(WorkspacePolicy, object())
    runtime = _FailOnceRuntime()
    owner = cli._RuntimeOwner(policy, builder=lambda _identity, _policy: runtime)
    created = owner.create((WorkspaceKind.GIT, Path("/data/one")))
    assert created is runtime

    with pytest.raises(RuntimeError, match="stop rejected"):
        owner.stop(runtime)
    assert runtime.stop_calls == 1

    owner.stop(runtime)
    assert runtime.stop_calls == 2

    # Ownership was already released by the successful stop above; a further
    # call is a no-op and must not invoke the underlying runtime again.
    owner.stop(runtime)
    assert runtime.stop_calls == 2


def test_runtime_owner_and_service_compose_fail_once_stop_without_false_idle() -> None:
    """Two real ``_RuntimeOwner.stop`` calls compose with service retry.

    The first ``release_lease`` triggers a real stop that fails; the service
    must not raise and must report non-idle.  A later ``sweep`` retries with a
    second real stop call that succeeds, and only then does the daemon become
    idle (findings 1 and 2).
    """

    policy = cast(WorkspacePolicy, object())
    runtime = _FailOnceRuntime()
    owner = cli._RuntimeOwner(policy, builder=lambda _identity, _policy: runtime)
    registry = WorkspaceRuntimeRegistry[cli.PhysicalWorkspaceKey, cli._Runtime, UUID](owner.create)
    service = WorkspaceDaemonService[cli.PhysicalWorkspaceKey, cli._Runtime](
        lifecycle=LeaseLifecycle(clock=time.monotonic),
        registry=registry,
        resolver=_single_root_resolution,
        runtime_stopper=owner.stop,
    )

    async def scenario() -> None:
        grant = await service.acquire_lease(mcp_session_id="session")
        lease_id = grant["lease_id"]
        assert isinstance(lease_id, str)
        await service.activate_workspace(lease_id=lease_id, absolute_path="/data/one")

        released = await service.release_lease(lease_id=lease_id, immediate=True)
        # The first real stop call failed, so the release must truthfully
        # report its own target as still pending, never as stopped.
        assert released["runtime_stopped"] is False
        assert released["runtime_stop_pending"] is True
        assert runtime.stop_calls == 1
        assert service.daemon_idle() is False

        await service.sweep()
        assert runtime.stop_calls == 2
        assert service.daemon_idle() is True

    asyncio.run(scenario())
