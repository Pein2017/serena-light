from __future__ import annotations

import asyncio
import os
import socket
import sys
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

import pytest

from serena_light import __version__, cli
from serena_light.runtime_files import BearerSecret, DiscoveryMetadata


def _metadata(*, daemon_id: str | None = None) -> DiscoveryMetadata:
    return DiscoveryMetadata.create(
        daemon_id=daemon_id or str(uuid4()),
        pid=os.getpid(),
        process_start_time=10.0,
        endpoint="http://127.0.0.1:43123/mcp",
        protocol_version="2025-11-25",
        server_version=__version__,
    )


def test_daemon_command_uses_current_interpreter_module_and_sanitized_environment() -> None:
    assert cli._daemon_argv() == (
        sys.executable,
        "-I",
        "-m",
        "serena_light.cli",
        "daemon",
    )
    environment = cli._daemon_environment(
        {
            "PATH": "/ambient/bin",
            "PYTHONHOME": "/root/python",
            "PYTHONPATH": "/root/site",
            "KEEP": "yes",
        }
    )
    assert environment == {
        "PATH": "/ambient/bin",
        "KEEP": "yes",
        "PYTHONNOUSERSITE": "1",
    }


def test_detached_daemon_spawn_receives_explicit_safe_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setattr(cli, "_daemon_environment", lambda: {"PYTHONNOUSERSITE": "1"})

    cli._spawn_daemon()

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
                },
            }

    def fake_get(url: str, **kwargs: object) -> Response:
        captured.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(cli.httpx, "get", fake_get)

    assert cli._daemon_is_healthy(candidate)
    assert captured["url"] == "http://127.0.0.1:43123/health"
    assert captured["headers"] == {"Authorization": "Bearer " + "a" * 48}


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
