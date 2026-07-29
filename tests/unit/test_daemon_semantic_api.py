from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from uuid import uuid4

from starlette.testclient import TestClient

from serena_light.daemon.leases import LeaseLifecycle
from serena_light.daemon.server import create_daemon_app
from serena_light.daemon.service import WorkspaceDaemonService
from serena_light.lsp.adapter import AdapterError, AdapterErrorCode
from serena_light.lsp.executor import ExecutorBusyError
from serena_light.runtime_files import BearerSecret
from serena_light.tools.envelopes import ErrorCode, JsonValue, error, success
from serena_light.workspace.identity import WorkspaceError, WorkspaceErrorCode, WorkspaceErrorData
from serena_light.workspace.registry import ResolvedWorkspace, WorkspaceRuntimeRegistry
from serena_light.workspace.runtime import RuntimeErrorCode, WorkspaceRuntimeError


@dataclass(eq=False)
class FakeRuntime:
    identity: str
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def status(self) -> Mapping[str, object]:
        return {"identity": self.identity, "executor": {"queue_size": 0}}

    def find_symbol(self, **kwargs: object) -> object:
        self.calls.append(("find_symbol", dict(kwargs)))
        return success(cast(JsonValue, {"identity": self.identity, "query": kwargs["name_path"]}))


@dataclass(slots=True)
class OutcomeRuntime:
    outcome: object

    def operation(self) -> object:
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _service() -> tuple[WorkspaceDaemonService[str, FakeRuntime], list[FakeRuntime]]:
    created: list[FakeRuntime] = []

    def factory(identity: str) -> FakeRuntime:
        runtime = FakeRuntime(identity)
        created.append(runtime)
        return runtime

    service = WorkspaceDaemonService[str, FakeRuntime](
        lifecycle=LeaseLifecycle(clock=lambda: 0.0),
        registry=WorkspaceRuntimeRegistry(factory),
        resolver=lambda path: ResolvedWorkspace(identity=str(path.parent), working_subdirectory=path),
        runtime_stopper=lambda _runtime: None,
    )
    return service, created


def _initialize(client: TestClient, authorization: str) -> str:
    response = client.post(
        "/mcp",
        headers={"Authorization": authorization, "Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.headers["mcp-session-id"]


def _call(
    client: TestClient,
    authorization: str,
    session_id: str,
    name: str,
    arguments: Mapping[str, object] | None = None,
    lease_id: str | None = None,
) -> dict[str, object]:
    params: dict[str, object] = {"name": name, "arguments": dict(arguments or {})}
    if lease_id is not None:
        params["_meta"] = {"serena_light": {"lease_id": lease_id}}
    response = client.post(
        "/mcp",
        headers={
            "Authorization": authorization,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Mcp-Session-Id": session_id,
            "Mcp-Protocol-Version": "2025-11-25",
        },
        json={"jsonrpc": "2.0", "id": uuid4().int, "method": "tools/call", "params": params},
    )
    assert response.status_code == 200, response.text
    return cast(dict[str, object], response.json()["result"]["structuredContent"])


def _data(value: Mapping[str, object]) -> Mapping[str, object]:
    assert value["ok"] is True
    return cast(Mapping[str, object], value["data"])


def test_public_semantic_tools_bind_only_through_meta_and_preserve_envelopes() -> None:
    service, created = _service()
    token = "t" * 48
    daemon_id = str(uuid4())
    app = create_daemon_app(service=service, bearer=BearerSecret(token), daemon_id=daemon_id)
    authorization = f"Bearer {token}"

    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)) as client:
        session = _initialize(client, authorization)
        listed = client.post(
            "/mcp",
            headers={
                "Authorization": authorization,
                "Accept": "application/json, text/event-stream",
                "Mcp-Session-Id": session,
                "Mcp-Protocol-Version": "2025-11-25",
            },
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        names = {tool["name"] for tool in listed.json()["result"]["tools"]}
        assert {
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
        } <= names

        lease = _data(_call(client, authorization, session, "acquire_lease"))["lease_id"]
        assert isinstance(lease, str)
        activation = _data(
            _call(
                client,
                authorization,
                session,
                "activate_workspace",
                {"absolute_path": "/data/one/subdir"},
                lease,
            )
        )
        assert activation["lease_id"] == lease
        result = _data(_call(client, authorization, session, "find_symbol", {"name_path": "Thing"}, lease))
        assert result == {"identity": "/data/one", "query": "Thing"}
        assert created[0].calls == [
            (
                "find_symbol",
                {
                    "name_path": "Thing",
                    "relative_path": None,
                    "substring_matching": False,
                    "include_body": False,
                    "include_info": False,
                    "max_answer_chars": 12_000,
                },
            )
        ]

        status = _data(_call(client, authorization, session, "get_runtime_status", lease_id=lease))
        assert status["daemon_id"] == daemon_id
        assert _data_map(status["binding"])["working_subdirectory"] == "/data/one/subdir"
        assert _data_map(status["runtime"])["identity"] == "/data/one"
        assert token not in json.dumps(status)

        missing = _call(client, authorization, session, "find_symbol", {"name_path": "Thing"})
        assert _data_map(missing["error"])["code"] == "LEASE_EXPIRED"

        unsupported = _call(
            client,
            authorization,
            session,
            "replace_symbol_body",
            {"name_path": "Thing", "relative_path": "a.py", "body": "x", "expected_hash": "0" * 64},
            lease,
        )
        assert _data_map(unsupported["error"])["code"] == "UNSUPPORTED"


def test_release_workspace_keeps_lease_live_and_bindings_isolated() -> None:
    service, created = _service()

    async def scenario() -> None:
        first = cast(str, (await service.acquire_lease(mcp_session_id="a"))["lease_id"])
        second = cast(str, (await service.acquire_lease(mcp_session_id="b"))["lease_id"])
        await service.activate_workspace(lease_id=first, absolute_path="/data/one/child")
        await service.activate_workspace(lease_id=second, absolute_path="/data/two/child")

        await service.semantic_operation(lease_id=first, operation="find_symbol", name_path="One")
        await service.semantic_operation(lease_id=second, operation="find_symbol", name_path="Two")
        assert [runtime.calls for runtime in created] == [
            [("find_symbol", {"name_path": "One"})],
            [("find_symbol", {"name_path": "Two"})],
        ]

        released = await service.release_workspace(lease_id=first)
        assert released["released"] is True
        assert (await service.heartbeat(lease_id=first))["lease_id"] == first
        unbound = await service.get_runtime_status(lease_id=first)
        assert _data_map(unbound["data"])["binding"] is None
        await service.activate_workspace(lease_id=first, absolute_path="/data/three/child")
        assert (await service.binding_for(lease_id=first)).identity == "/data/three"

    asyncio.run(scenario())


def test_http_activation_rejections_are_typed_and_keep_the_prior_binding() -> None:
    created: list[FakeRuntime] = []

    def factory(identity: str) -> FakeRuntime:
        runtime = FakeRuntime(identity)
        created.append(runtime)
        return runtime

    def resolver(path: Path) -> ResolvedWorkspace[str]:
        if path == Path("/data/missing"):
            raise WorkspaceError(WorkspaceErrorData(WorkspaceErrorCode.INVALID_PATH, "missing", path=path))
        if path == Path("/outside/untrusted"):
            raise WorkspaceError(WorkspaceErrorData(WorkspaceErrorCode.UNTRUSTED_ROOT, "untrusted", path=path))
        return ResolvedWorkspace(identity=str(path.parent), working_subdirectory=path)

    service = WorkspaceDaemonService[str, FakeRuntime](
        lifecycle=LeaseLifecycle(clock=lambda: 0.0),
        registry=WorkspaceRuntimeRegistry(factory),
        resolver=resolver,
        runtime_stopper=lambda _runtime: None,
    )
    token = "t" * 48
    authorization = f"Bearer {token}"
    app = create_daemon_app(service=service, bearer=BearerSecret(token), daemon_id=str(uuid4()))

    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)) as client:
        session = _initialize(client, authorization)
        lease = _data(_call(client, authorization, session, "acquire_lease"))["lease_id"]
        assert isinstance(lease, str)
        _data(
            _call(
                client,
                authorization,
                session,
                "activate_workspace",
                {"absolute_path": "/data/active/subdir"},
                lease,
            )
        )

        for path, code in (
            ("relative/path", "INVALID_PATH"),
            ("/data/missing", "INVALID_PATH"),
            ("/outside/untrusted", "UNTRUSTED_ROOT"),
        ):
            rejected = _call(
                client,
                authorization,
                session,
                "activate_workspace",
                {"absolute_path": path},
                lease,
            )
            assert rejected["ok"] is False
            assert _data_map(rejected["error"])["code"] == code

        status = _data(_call(client, authorization, session, "get_runtime_status", lease_id=lease))
        assert _data_map(status["binding"])["working_subdirectory"] == "/data/active/subdir"
        assert _data(_call(client, authorization, session, "heartbeat", {"lease_id": lease}))["lease_id"] == lease
        assert [runtime.identity for runtime in created] == ["/data/active"]


def test_http_immediate_workspace_release_stops_only_the_last_holder() -> None:
    created: list[FakeRuntime] = []
    stopped: list[FakeRuntime] = []

    def factory(identity: str) -> FakeRuntime:
        runtime = FakeRuntime(identity)
        created.append(runtime)
        return runtime

    service = WorkspaceDaemonService[str, FakeRuntime](
        lifecycle=LeaseLifecycle(clock=lambda: 0.0),
        registry=WorkspaceRuntimeRegistry(factory),
        resolver=lambda path: ResolvedWorkspace(identity=str(path.parent), working_subdirectory=path),
        runtime_stopper=stopped.append,
    )
    token = "t" * 48
    authorization = f"Bearer {token}"
    app = create_daemon_app(service=service, bearer=BearerSecret(token), daemon_id=str(uuid4()))

    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)) as client:
        session = _initialize(client, authorization)
        first = _data(_call(client, authorization, session, "acquire_lease"))["lease_id"]
        second = _data(_call(client, authorization, session, "acquire_lease"))["lease_id"]
        assert isinstance(first, str)
        assert isinstance(second, str)
        for lease in (first, second):
            _data(
                _call(
                    client,
                    authorization,
                    session,
                    "activate_workspace",
                    {"absolute_path": "/data/project/subdir"},
                    lease,
                )
            )

        non_last = _data(
            _call(client, authorization, session, "release_workspace", {"immediate": True}, first)
        )
        assert non_last["active_holders"] == 1
        assert non_last["immediate"] is True
        assert non_last["runtime_stopped"] is False
        assert stopped == []
        assert _data(_call(client, authorization, session, "find_symbol", {"name_path": "Thing"}, second))[
            "identity"
        ] == "/data/project"

        last = _data(
            _call(client, authorization, session, "release_workspace", {"immediate": True}, second)
        )
        assert last["active_holders"] == 0
        assert last["immediate"] is True
        assert last["runtime_stopped"] is True
        assert [runtime.identity for runtime in stopped] == ["/data/project"]


def test_http_release_workspace_rejects_non_boolean_immediate() -> None:
    service, _created = _service()
    token = "t" * 48
    authorization = f"Bearer {token}"
    app = create_daemon_app(service=service, bearer=BearerSecret(token), daemon_id=str(uuid4()))

    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)) as client:
        session = _initialize(client, authorization)
        lease = _data(_call(client, authorization, session, "acquire_lease"))["lease_id"]
        assert isinstance(lease, str)
        response = client.post(
            "/mcp",
            headers={
                "Authorization": authorization,
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Mcp-Session-Id": session,
                "Mcp-Protocol-Version": "2025-11-25",
            },
            json={
                "jsonrpc": "2.0",
                "id": uuid4().int,
                "method": "tools/call",
                "params": {
                    "name": "release_workspace",
                    "arguments": {"immediate": "true"},
                    "_meta": {"serena_light": {"lease_id": lease}},
                },
            },
        )
        assert response.status_code == 200
        result = cast(Mapping[str, object], response.json()["result"])

    assert result["isError"] is True
    content = result["content"]
    assert isinstance(content, list)
    assert "valid boolean" in str(content)


def test_runtime_boundary_converts_typed_failures_without_generic_rewriting() -> None:
    service, _created = _service()

    async def call(outcome: object) -> Mapping[str, object]:
        return await service._runtime_result(cast(FakeRuntime, OutcomeRuntime(outcome)), "operation", {})

    for code in WorkspaceErrorCode:
        converted = asyncio.run(call(WorkspaceError(WorkspaceErrorData(code, "secret"))))
        assert _data_map(converted["error"])["code"] == code.value

    assert _data_map(asyncio.run(call(ExecutorBusyError("secret")))["error"])["code"] == "BUSY"
    assert _data_map(
        asyncio.run(call(AdapterError(AdapterErrorCode.COOLDOWN, "secret", retry_after_seconds=1)))["error"]
    )["code"] == "COOLDOWN"
    assert _data_map(
        asyncio.run(call(WorkspaceRuntimeError(RuntimeErrorCode.SCOPE_INCOMPATIBLE, "secret")))["error"]
    )["code"] == "SCOPE_INCOMPATIBLE"
    assert _data_map(asyncio.run(call(TimeoutError("secret")))["error"])["code"] == "TIMED_OUT"
    assert _data_map(asyncio.run(call(OSError("secret")))["error"])["code"] == "UNCERTAIN"
    uncertain = error(ErrorCode.UNCERTAIN).to_dict()
    assert asyncio.run(call(uncertain)) == uncertain


def test_runtime_boundary_rejects_malformed_runtime_mappings() -> None:
    service, _created = _service()

    async def scenario() -> Mapping[str, object]:
        return await service._runtime_result(
            cast(FakeRuntime, OutcomeRuntime({"result": "not an envelope"})), "operation", {}
        )

    converted = asyncio.run(scenario())
    assert _data_map(converted["error"]) == {
        "code": "UNSUPPORTED",
        "message": "operation is unsupported",
        "retry": None,
        "details": {"tool": "operation", "reason": "malformed_runtime_result"},
    }


def _data_map(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return cast(Mapping[str, object], value)
