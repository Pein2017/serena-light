from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from uuid import uuid4

from starlette.testclient import TestClient

from serena_light.daemon.server import DaemonService, LeaseExpiredError, create_daemon_app
from serena_light.instructions import AGENT_INSTRUCTIONS
from serena_light.runtime_files import BearerSecret


class SessionService:
    def __init__(self, daemon_id: str) -> None:
        self.daemon_id = daemon_id
        self.sessions: set[str] = set()
        self.leases: set[str] = set()

    async def status(self, *, mcp_session_id: str) -> Mapping[str, object]:
        self.sessions.add(mcp_session_id)
        return {"session_count": len(self.sessions)}

    async def migration_status(self) -> Mapping[str, object]:
        return {"active_holders": len(self.leases), "daemon_idle": not self.leases}

    async def acquire_lease(self, *, mcp_session_id: str) -> Mapping[str, object]:
        self.sessions.add(mcp_session_id)
        lease_id = str(uuid4())
        self.leases.add(lease_id)
        return {"lease_id": lease_id}

    async def heartbeat(self, *, lease_id: str) -> Mapping[str, object]:
        if lease_id not in self.leases:
            raise LeaseExpiredError
        return {"lease_id": lease_id, "active": True}

    async def release_lease(self, *, lease_id: str, immediate: bool) -> Mapping[str, object]:
        if lease_id not in self.leases:
            raise LeaseExpiredError
        self.leases.discard(lease_id)
        return {"lease_id": lease_id, "released": True, "immediate": immediate}

    async def activate_workspace(self, *, lease_id: str, absolute_path: str) -> Mapping[str, object]:
        if lease_id not in self.leases:
            raise LeaseExpiredError
        return {"lease_id": lease_id, "workspace": absolute_path}


def _initialize(client: TestClient, authorization: str, request_id: int) -> str:
    response = client.post(
        "/mcp",
        headers={
            "Authorization": authorization,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": f"test-{request_id}", "version": "1"},
            },
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["result"]["instructions"] == AGENT_INSTRUCTIONS
    return response.headers["mcp-session-id"]


def _call_tool(
    client: TestClient,
    authorization: str,
    session_id: str,
    request_id: int,
    name: str,
    arguments: dict[str, object] | None = None,
    meta: dict[str, object] | None = None,
) -> dict[str, object]:
    params: dict[str, object] = {"name": name, "arguments": arguments or {}}
    if meta is not None:
        params["_meta"] = meta
    response = client.post(
        "/mcp",
        headers={
            "Authorization": authorization,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Mcp-Session-Id": session_id,
            "Mcp-Protocol-Version": "2025-11-25",
        },
        json={"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    result = payload["result"]
    assert isinstance(result, dict)
    structured = result["structuredContent"]
    assert isinstance(structured, dict)
    return structured


def _list_tools(client: TestClient, authorization: str, session_id: str) -> list[Mapping[str, object]]:
    response = client.post(
        "/mcp",
        headers={
            "Authorization": authorization,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Mcp-Session-Id": session_id,
            "Mcp-Protocol-Version": "2025-11-25",
        },
        json={"jsonrpc": "2.0", "id": 11, "method": "tools/list", "params": {}},
    )
    assert response.status_code == 200, response.text
    tools = response.json()["result"]["tools"]
    assert isinstance(tools, list)
    return [cast(Mapping[str, object], tool) for tool in tools]


def _mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return cast(Mapping[str, object], value)


def test_two_authenticated_streamable_http_sessions_have_distinct_leases() -> None:
    daemon_id = str(uuid4())
    service = SessionService(daemon_id)
    token = "s" * 48
    app = create_daemon_app(
        service=cast(DaemonService, service),
        bearer=BearerSecret(token),
        daemon_id=daemon_id,
    )
    authorization = f"Bearer {token}"

    with TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 50000),
    ) as client:
        session_a = _initialize(client, authorization, 1)
        session_b = _initialize(client, authorization, 2)
        tools = _list_tools(client, authorization, session_a)
        assert {tool["name"] for tool in tools} == {
            "get_daemon_status",
            "acquire_lease",
            "heartbeat",
            "release_lease",
            "activate_workspace",
            "release_workspace",
            "get_runtime_status",
            "get_symbols_overview",
            "find_symbol",
            "find_declaration",
            "find_implementations",
            "find_referencing_symbols",
            "get_diagnostics_for_file",
            "get_diagnostics_for_symbol",
            "replace_symbol_body",
        }
        assert all(isinstance(tool.get("description"), str) and tool["description"] for tool in tools)
        activation = next(tool for tool in tools if tool["name"] == "activate_workspace")
        activation_schema = _mapping(activation["inputSchema"])
        assert activation_schema["required"] == ["absolute_path"]
        assert "lease_id" not in _mapping(activation_schema["properties"])
        declaration = next(tool for tool in tools if tool["name"] == "find_declaration")
        declaration_properties = _mapping(_mapping(declaration["inputSchema"])["properties"])
        regex_schema = _mapping(declaration_properties["regex"])
        assert "exactly one capture group" in str(regex_schema["description"])
        lease_a = _call_tool(client, authorization, session_a, 3, "acquire_lease")
        lease_b = _call_tool(client, authorization, session_b, 4, "acquire_lease")
        status_a = _call_tool(client, authorization, session_a, 5, "get_daemon_status")
        migration = client.get("/migration-status", headers={"Authorization": authorization})

        assert lease_a["ok"] is True
        assert lease_b["ok"] is True
        data_a = _mapping(lease_a["data"])
        data_b = _mapping(lease_b["data"])
        assert data_a["lease_id"] != data_b["lease_id"]
        assert data_a["daemon_id"] == data_b["daemon_id"] == daemon_id
        assert _mapping(status_a["data"])["session_count"] == 2
        assert migration.status_code == 200
        migration_data = _mapping(_mapping(migration.json())["data"])
        assert migration_data["daemon_id"] == daemon_id
        assert migration_data["active_holders"] == 2
        assert migration_data["daemon_idle"] is False
        assert isinstance(migration_data["pid"], int)
        assert isinstance(migration_data["process_start_time"], float)

        activated = _call_tool(
            client,
            authorization,
            session_a,
            6,
            "activate_workspace",
            {"absolute_path": "/data/CoordExp"},
            {"serena_light": {"lease_id": data_a["lease_id"]}},
        )
        assert _mapping(activated["data"])["lease_id"] == data_a["lease_id"]

        missing = _call_tool(
            client,
            authorization,
            session_a,
            7,
            "activate_workspace",
            {"absolute_path": "/data/CoordExp"},
        )
        assert missing["ok"] is False
        assert _mapping(missing["error"])["code"] == "LEASE_EXPIRED"

        released = _call_tool(
            client,
            authorization,
            session_a,
            8,
            "release_lease",
            {"lease_id": data_a["lease_id"]},
        )
        assert released["ok"] is True
        expired = _call_tool(
            client,
            authorization,
            session_a,
            9,
            "heartbeat",
            {"lease_id": data_a["lease_id"]},
        )
        assert _mapping(expired["error"])["code"] == "LEASE_EXPIRED"
        retained = _call_tool(
            client,
            authorization,
            session_b,
            10,
            "heartbeat",
            {"lease_id": data_b["lease_id"]},
        )
        assert _mapping(retained["data"])["active"] is True


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_starter_process_exits_while_detached_daemon_remains_healthy(tmp_path: Path) -> None:
    port = _free_loopback_port()
    daemon_id = str(uuid4())
    token = "d" * 48
    pid_path = tmp_path / "daemon.pid"
    daemon_code = """
import os
from collections.abc import Mapping
import uvicorn
from serena_light.daemon.server import create_daemon_app
from serena_light.runtime_files import BearerSecret
class Service:
    async def status(self, *, mcp_session_id: str) -> Mapping[str, object]: return {}
    async def acquire_lease(self, *, mcp_session_id: str) -> Mapping[str, object]:
        return {"lease_id": os.environ["LEASE_ID"]}
    async def heartbeat(self, *, lease_id: str) -> Mapping[str, object]: return {"lease_id": lease_id}
    async def release_lease(self, *, lease_id: str, immediate: bool) -> Mapping[str, object]:
        return {"lease_id": lease_id}
    async def activate_workspace(self, *, lease_id: str, absolute_path: str) -> Mapping[str, object]:
        return {"lease_id": lease_id}
app = create_daemon_app(service=Service(), bearer=BearerSecret(os.environ["TOKEN"]), daemon_id=os.environ["DAEMON_ID"])
uvicorn.run(app, host="127.0.0.1", port=int(os.environ["PORT"]), log_level="critical")
"""
    starter_code = """
import json, os
from pathlib import Path
from serena_light.daemon.server import spawn_detached_process
process = spawn_detached_process([os.environ["PYTHON"], "-c", os.environ["DAEMON_CODE"]], env=os.environ)
Path(os.environ["PID_PATH"]).write_text(json.dumps({"pid": process.pid}))
"""
    env = dict(os.environ)
    env.update(
        {
            "PYTHON": sys.executable,
            "DAEMON_CODE": daemon_code,
            "PID_PATH": os.fspath(pid_path),
            "PORT": str(port),
            "TOKEN": token,
            "DAEMON_ID": daemon_id,
            "LEASE_ID": str(uuid4()),
        }
    )
    starter = subprocess.run(
        [sys.executable, "-c", starter_code],
        env=env,
        cwd=tmp_path,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert starter.returncode == 0
    daemon_pid = int(json.loads(pid_path.read_text())["pid"])
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                with urllib.request.urlopen(request, timeout=0.2) as response:
                    health = json.load(response)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        assert health["data"]["daemon_id"] == daemon_id
        assert Path(f"/proc/{daemon_pid}").exists()
        assert os.getsid(daemon_pid) == daemon_pid
    finally:
        os.kill(daemon_pid, signal.SIGTERM)
        deadline = time.monotonic() + 3
        while Path(f"/proc/{daemon_pid}").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if Path(f"/proc/{daemon_pid}").exists():
            os.kill(daemon_pid, signal.SIGKILL)
