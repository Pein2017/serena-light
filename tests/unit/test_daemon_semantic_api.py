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
from serena_light.lsp.adapter import AdapterError, AdapterErrorCode, LspProcessLost
from serena_light.lsp.client import LspProtocolError, LspResponseError, LspTransportClosed
from serena_light.lsp.executor import ExecutorBusyError
from serena_light.runtime_files import BearerSecret
from serena_light.tools.envelopes import (
    AdapterMetadata,
    ErrorCode,
    GenerationMetadata,
    JsonValue,
    WorkspaceMetadata,
    error,
    success,
)
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
        return success(
            cast(
                JsonValue,
                {
                    "relative_path": "src/main.py",
                    "sha256": "a" * 64,
                    "symbol": {
                        "name_path": kwargs["name_path"],
                        "kind": 5,
                        "range": {
                            "start": {"line": 0, "column": 0, "text_offset": 0, "byte_offset": 0},
                            "end": {"line": 0, "column": 5, "text_offset": 5, "byte_offset": 5},
                        },
                    },
                },
            ),
            workspace=WorkspaceMetadata(self.identity, "git", self.identity),
            adapter=AdapterMetadata("pyright", "python"),
            generations=GenerationMetadata(trust=1, program=1, document=1, index=1),
        )

    def find_implementations(self, **kwargs: object) -> object:
        self.calls.append(("find_implementations", dict(kwargs)))
        included = set(cast(list[int] | None, kwargs.get("include_kinds")) or ())
        excluded = set(cast(list[int] | None, kwargs.get("exclude_kinds")) or ())
        locations = [
            {
                "relative_path": "src/runner.ts",
                "name_path": "Runner",
                "kind": 5,
                "range": {
                    "start": {"line": 0, "column": 0, "text_offset": 0, "byte_offset": 0},
                    "end": {"line": 0, "column": 6, "text_offset": 6, "byte_offset": 6},
                },
            },
            {
                "relative_path": "src/runner.ts",
                "name_path": "Runner.run",
                "kind": 6,
                "range": {
                    "start": {"line": 1, "column": 0, "text_offset": 7, "byte_offset": 7},
                    "end": {"line": 1, "column": 3, "text_offset": 10, "byte_offset": 10},
                },
            },
        ]
        return success(
            cast(
                JsonValue,
                {
                    "relative_path": "src/main.py",
                    "name_path": "Runner",
                    "locations": [
                        location
                        for location in locations
                        if (not included or location["kind"] in included)
                        and location["kind"] not in excluded
                    ],
                },
            ),
            workspace=WorkspaceMetadata(self.identity, "git", self.identity),
            adapter=AdapterMetadata("pyright", "python"),
            generations=GenerationMetadata(trust=1, program=1, document=1, index=1),
        )


@dataclass(slots=True)
class OutcomeRuntime:
    outcome: object

    def operation(self) -> object:
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    def replace_symbol_body(self) -> object:
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
        assert result == {
            "workspace": "/data/one",
            "files": [
                {
                    "path": "src/main.py",
                    "symbols": [{"name_path": "Thing", "kind": "class", "range": [[0, 0], [0, 5]]}],
                }
            ],
            "omitted": 0,
        }
        assert created[0].calls == [
            (
                "find_symbol",
                {
                    "name_path": "Thing",
                    "relative_path": None,
                    "substring_matching": False,
                        "include_body": False,
                        "include_info": False,
                        "max_answer_chars": 2_147_483_647,
                        "_error_max_answer_chars": 12_000,
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


def test_public_implementation_kind_filters_reach_the_bound_runtime() -> None:
    service, created = _service()
    token = "u" * 48
    app = create_daemon_app(service=service, bearer=BearerSecret(token), daemon_id=str(uuid4()))
    authorization = f"Bearer {token}"

    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50010)) as client:
        session = _initialize(client, authorization)
        lease = _data(_call(client, authorization, session, "acquire_lease"))["lease_id"]
        assert isinstance(lease, str)
        _call(client, authorization, session, "activate_workspace", {"absolute_path": "/data/one/subdir"}, lease)

        result = _data(
            _call(
                client,
                authorization,
                session,
                "find_implementations",
                {
                    "name_path": "Runner",
                    "relative_path": "src/main.py",
                    "include_kinds": [5, 6],
                    "exclude_kinds": [5],
                },
                lease,
            )
        )

    assert result["files"] == [
        {
            "path": "src/runner.ts",
            "targets": [
                {
                    "range": [[1, 0], [1, 3]],
                    "name_path": "Runner.run",
                    "kind": "method",
                }
            ],
        }
    ]
    assert created[0].calls[-1] == (
        "find_implementations",
        {
            "name_path": "Runner",
            "relative_path": "src/main.py",
            "include_info": False,
            "include_kinds": [5, 6],
            "exclude_kinds": [5],
            "max_answer_chars": 2_147_483_647,
        },
    )


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
        assert non_last["runtime_stop_pending"] is False
        assert stopped == []
        assert _data(_call(client, authorization, session, "find_symbol", {"name_path": "Thing"}, second))[
            "workspace"
        ] == "/data/project"

        last = _data(
            _call(client, authorization, session, "release_workspace", {"immediate": True}, second)
        )
        assert last["active_holders"] == 0
        assert last["immediate"] is True
        assert last["runtime_stopped"] is True
        assert last["runtime_stop_pending"] is False
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

    async def call(outcome: object, *, operation: str = "operation") -> Mapping[str, object]:
        return await service._runtime_result(cast(FakeRuntime, OutcomeRuntime(outcome)), operation, {})

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

    # A read can never have installed anything, so an ordinary LSP failure is
    # declared UNSUPPORTED with a bounded reason -- not a code that could
    # imply a possible write.
    lsp_response_read = asyncio.run(call(LspResponseError(-32603, "internal error: secret")))
    assert _data_map(lsp_response_read["error"]) == {
        "code": "UNSUPPORTED",
        "message": "operation is unsupported",
        "retry": None,
        "details": {"tool": "operation", "reason": "lsp_failure"},
    }
    assert "secret" not in json.dumps(lsp_response_read)

    # A write must fail conservatively since it may already be installed.
    lsp_protocol_edit = asyncio.run(
        call(LspProtocolError("malformed frame: secret"), operation="replace_symbol_body")
    )
    assert _data_map(lsp_protocol_edit["error"])["code"] == "UNCERTAIN"
    assert "secret" not in json.dumps(lsp_protocol_edit)

    # A transport close/process loss the adapter's own retry already
    # exhausted is the same boundary concern as an ordinary LSP failure --
    # not a programming error, and not the generic OSError fallback.
    transport_closed_read = asyncio.run(call(LspTransportClosed("adapter runtime disappeared: secret")))
    assert _data_map(transport_closed_read["error"]) == {
        "code": "UNSUPPORTED",
        "message": "operation is unsupported",
        "retry": None,
        "details": {"tool": "operation", "reason": "lsp_failure"},
    }
    assert "secret" not in json.dumps(transport_closed_read)

    process_lost_edit = asyncio.run(
        call(LspProcessLost("language server exited with status 1: secret"), operation="replace_symbol_body")
    )
    assert _data_map(process_lost_edit["error"]) == {
        "code": "UNCERTAIN",
        "message": "operation outcome is uncertain",
        "retry": None,
        "details": {},
    }
    assert "secret" not in json.dumps(process_lost_edit)

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


@dataclass(eq=False)
class LspFailingRuntime:
    """A runtime whose semantic read and pre-install edit resolution both
    surface ordinary LSP failures instead of a wrapped workspace/adapter error."""

    identity: str
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def find_symbol(self, **kwargs: object) -> object:
        self.calls.append(("find_symbol", dict(kwargs)))
        raise LspResponseError(-32603, "internal error: secret-token")

    def replace_symbol_body(self, **kwargs: object) -> object:
        # Fails while resolving the edit target, before any replacement is
        # installed -- there is nothing to replay.
        self.calls.append(("replace_symbol_body", dict(kwargs)))
        raise LspProtocolError("pre-install resolution failed: secret-token")


def _lsp_failing_service() -> tuple[WorkspaceDaemonService[str, LspFailingRuntime], list[LspFailingRuntime]]:
    created: list[LspFailingRuntime] = []

    def factory(identity: str) -> LspFailingRuntime:
        runtime = LspFailingRuntime(identity)
        created.append(runtime)
        return runtime

    service = WorkspaceDaemonService[str, LspFailingRuntime](
        lifecycle=LeaseLifecycle(clock=lambda: 0.0),
        registry=WorkspaceRuntimeRegistry(factory),
        resolver=lambda path: ResolvedWorkspace(identity=str(path.parent), working_subdirectory=path),
        runtime_stopper=lambda _runtime: None,
    )
    return service, created


def test_lsp_response_and_protocol_errors_translate_through_service_and_http_boundary_without_replay() -> None:
    service, created = _lsp_failing_service()
    token = "t" * 48
    daemon_id = str(uuid4())
    app = create_daemon_app(service=service, bearer=BearerSecret(token), daemon_id=daemon_id)
    authorization = f"Bearer {token}"

    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)) as client:
        session = _initialize(client, authorization)
        lease = _data(_call(client, authorization, session, "acquire_lease"))["lease_id"]
        assert isinstance(lease, str)
        _call(client, authorization, session, "activate_workspace", {"absolute_path": "/data/one/subdir"}, lease)

        # A read cannot have installed anything, so it is declared UNSUPPORTED
        # with a bounded reason -- never a code implying a possible write.
        read_failure = _call(client, authorization, session, "find_symbol", {"name_path": "Thing"}, lease)
        assert _data_map(read_failure["error"]) == {
            "code": "UNSUPPORTED",
            "message": "operation is unsupported",
            "retry": None,
            "details": {"tool": "find_symbol", "reason": "lsp_failure"},
        }
        assert "secret-token" not in json.dumps(read_failure)

        # A write must fail conservatively (it may already be installed).
        edit_failure = _call(
            client,
            authorization,
            session,
            "replace_symbol_body",
            {"name_path": "Thing", "relative_path": "a.py", "body": "x", "expected_hash": "0" * 64},
            lease,
        )
        assert _data_map(edit_failure["error"])["code"] == "UNCERTAIN"
        assert "secret-token" not in json.dumps(edit_failure)

        # Each failing operation ran exactly once: no automatic replay of the
        # pre-install edit resolution or the read.
        assert [call[0] for call in created[0].calls] == ["find_symbol", "replace_symbol_body"]


@dataclass(eq=False)
class LspTransportLostRuntime:
    """A runtime whose semantic read and edit both surface the exact
    transport/process-lost exceptions the adapter's own retry already
    exhausted, instead of a wrapped workspace/adapter error."""

    identity: str
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def find_symbol(self, **kwargs: object) -> object:
        self.calls.append(("find_symbol", dict(kwargs)))
        raise LspTransportClosed("adapter runtime disappeared before dispatch: secret-token")

    def replace_symbol_body(self, **kwargs: object) -> object:
        # Fails after the adapter's own retry is exhausted, before any
        # replacement is installed -- there is nothing to replay.
        self.calls.append(("replace_symbol_body", dict(kwargs)))
        raise LspProcessLost("language server exited with status 1: secret-token")


def _lsp_transport_lost_service() -> tuple[
    WorkspaceDaemonService[str, LspTransportLostRuntime], list[LspTransportLostRuntime]
]:
    created: list[LspTransportLostRuntime] = []

    def factory(identity: str) -> LspTransportLostRuntime:
        runtime = LspTransportLostRuntime(identity)
        created.append(runtime)
        return runtime

    service = WorkspaceDaemonService[str, LspTransportLostRuntime](
        lifecycle=LeaseLifecycle(clock=lambda: 0.0),
        registry=WorkspaceRuntimeRegistry(factory),
        resolver=lambda path: ResolvedWorkspace(identity=str(path.parent), working_subdirectory=path),
        runtime_stopper=lambda _runtime: None,
    )
    return service, created


def test_exhausted_transport_and_process_lost_translate_through_service_and_http_boundary_without_replay() -> None:
    service, created = _lsp_transport_lost_service()
    token = "t" * 48
    daemon_id = str(uuid4())
    app = create_daemon_app(service=service, bearer=BearerSecret(token), daemon_id=daemon_id)
    authorization = f"Bearer {token}"

    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)) as client:
        session = _initialize(client, authorization)
        lease = _data(_call(client, authorization, session, "acquire_lease"))["lease_id"]
        assert isinstance(lease, str)
        _call(client, authorization, session, "activate_workspace", {"absolute_path": "/data/one/subdir"}, lease)

        # A read cannot have installed anything, so an exhausted transport
        # loss is declared UNSUPPORTED with a bounded reason -- never a code
        # implying a possible write.
        read_failure = _call(client, authorization, session, "find_symbol", {"name_path": "Thing"}, lease)
        assert _data_map(read_failure["error"]) == {
            "code": "UNSUPPORTED",
            "message": "operation is unsupported",
            "retry": None,
            "details": {"tool": "find_symbol", "reason": "lsp_failure"},
        }
        assert "secret-token" not in json.dumps(read_failure)

        # A write must fail conservatively with no retry (it may already be
        # installed).
        edit_failure = _call(
            client,
            authorization,
            session,
            "replace_symbol_body",
            {"name_path": "Thing", "relative_path": "a.py", "body": "x", "expected_hash": "0" * 64},
            lease,
        )
        assert _data_map(edit_failure["error"]) == {
            "code": "UNCERTAIN",
            "message": "operation outcome is uncertain",
            "retry": None,
            "details": {},
        }
        assert "secret-token" not in json.dumps(edit_failure)

        # Each failing operation ran exactly once: no automatic replay of the
        # edit or the read after the adapter's own retry was exhausted.
        assert [call[0] for call in created[0].calls] == ["find_symbol", "replace_symbol_body"]
