from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

from serena_light import __version__
from serena_light.daemon.server import (
    DaemonConfigurationError,
    DaemonIdentityError,
    DaemonService,
    connect_or_start,
    create_daemon_app,
    spawn_detached_process,
    validate_health_identity,
)
from serena_light.runtime_files import BearerSecret, DiscoveryMetadata


class CountingService:
    def __init__(self) -> None:
        self.calls = 0

    async def status(self, *, mcp_session_id: str) -> Mapping[str, object]:
        self.calls += 1
        return {"transport_session": mcp_session_id}

    async def acquire_lease(self, *, mcp_session_id: str) -> Mapping[str, object]:
        self.calls += 1
        return {"lease_id": str(uuid4()), "transport_session": mcp_session_id}

    async def heartbeat(self, *, lease_id: str) -> Mapping[str, object]:
        self.calls += 1
        return {"lease_id": lease_id}

    async def release_lease(self, *, lease_id: str, immediate: bool) -> Mapping[str, object]:
        self.calls += 1
        return {"lease_id": lease_id, "immediate": immediate}

    async def activate_workspace(self, *, lease_id: str, absolute_path: str) -> Mapping[str, object]:
        self.calls += 1
        return {"lease_id": lease_id, "absolute_path": absolute_path}


def _runtime_root(tmp_path: Path) -> Path:
    root = tmp_path / "runtime" / "serena-light"
    root.parent.mkdir(parents=True)
    return root


def _metadata(daemon_id: str) -> DiscoveryMetadata:
    return DiscoveryMetadata.create(
        daemon_id=daemon_id,
        pid=os.getpid(),
        process_start_time=10.0,
        endpoint="http://127.0.0.1:43123/mcp",
        protocol_version="2025-11-25",
        server_version=__version__,
    )


def test_bearer_rejection_precedes_every_service_callback() -> None:
    service = CountingService()
    daemon_id = str(uuid4())
    app = create_daemon_app(
        service=cast(DaemonService, service),
        bearer=BearerSecret("a" * 48),
        daemon_id=daemon_id,
    )

    with TestClient(app) as client:
        missing = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        wrong = client.get("/health", headers={"Authorization": "Bearer wrong"})
        accepted = client.get("/health", headers={"Authorization": f"Bearer {'a' * 48}"})

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert service.calls == 0
    assert accepted.status_code == 200
    assert accepted.json()["data"]["daemon_id"] == daemon_id


def test_non_loopback_or_invalid_identity_configuration_is_rejected() -> None:
    service = CountingService()
    secret = BearerSecret("a" * 48)
    with pytest.raises(DaemonConfigurationError, match="127.0.0.1"):
        create_daemon_app(
            service=cast(DaemonService, service), bearer=secret, daemon_id=str(uuid4()), host="0.0.0.0"
        )
    with pytest.raises(DaemonConfigurationError, match="UUID"):
        create_daemon_app(service=cast(DaemonService, service), bearer=secret, daemon_id="not-a-uuid")


def test_health_identity_and_versions_must_match_discovery() -> None:
    daemon_id = str(uuid4())
    metadata = _metadata(daemon_id)
    payload: dict[str, object] = {
        "ok": True,
        "data": {
            "daemon_id": daemon_id,
            "server_version": __version__,
            "protocol_version": "2025-11-25",
        },
    }
    assert validate_health_identity(metadata, payload).daemon_id == daemon_id

    data = payload["data"]
    assert isinstance(data, dict)
    data["server_version"] = "stale"
    with pytest.raises(DaemonIdentityError, match="does not match"):
        validate_health_identity(metadata, payload)


def test_simultaneous_connect_or_start_spawns_exactly_once(tmp_path: Path) -> None:
    root = _runtime_root(tmp_path)
    start = threading.Barrier(8)
    state: dict[str, Any] = {"candidate": None, "spawns": 0}
    state_lock = threading.Lock()
    results: list[str] = []

    def discover() -> str:
        with state_lock:
            candidate = state["candidate"]
        if candidate is None:
            raise FileNotFoundError
        assert isinstance(candidate, str)
        return candidate

    def spawn() -> None:
        with state_lock:
            state["spawns"] += 1
            state["candidate"] = "healthy-daemon"

    def connector() -> None:
        start.wait()
        result = connect_or_start(
            runtime_root=root,
            discover=discover,
            is_healthy=lambda candidate: candidate == "healthy-daemon",
            spawn=spawn,
            timeout_seconds=2.0,
            poll_seconds=0.005,
        )
        results.append(result)

    threads = [threading.Thread(target=connector) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert all(not thread.is_alive() for thread in threads)
    assert state["spawns"] == 1
    assert results == ["healthy-daemon"] * 8
    assert (root / "startup.lock").stat().st_mode & 0o777 == 0o600


def test_stale_discovery_is_replaced_then_reused(tmp_path: Path) -> None:
    root = _runtime_root(tmp_path)
    candidate = "stale"
    spawns = 0

    def spawn() -> None:
        nonlocal candidate, spawns
        spawns += 1
        candidate = "fresh"

    first = connect_or_start(
        runtime_root=root,
        discover=lambda: candidate,
        is_healthy=lambda candidate: candidate == "fresh",
        spawn=spawn,
        timeout_seconds=1.0,
        poll_seconds=0.005,
    )
    second = connect_or_start(
        runtime_root=root,
        discover=lambda: candidate,
        is_healthy=lambda candidate: candidate == "fresh",
        spawn=spawn,
        timeout_seconds=1.0,
        poll_seconds=0.005,
    )

    assert first == second == "fresh"
    assert spawns == 1


def test_detached_spawn_uses_new_session_devnull_and_no_fds(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class Process:
        pid = 43123

    def fake_popen(argv: tuple[str, ...], **kwargs: object) -> Process:
        captured["argv"] = argv
        captured.update(kwargs)
        return Process()

    monkeypatch.setattr("serena_light.daemon.server.subprocess.Popen", fake_popen)
    process = spawn_detached_process(["/usr/bin/python3", "-c", "pass"])

    assert process.pid == 43123
    assert captured["stdin"] == -3
    assert captured["stdout"] == -3
    assert captured["stderr"] == -3
    assert captured["close_fds"] is True
    assert captured["start_new_session"] is True
    assert captured["cwd"] == "/"
