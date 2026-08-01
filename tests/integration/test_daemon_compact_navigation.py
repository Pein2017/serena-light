from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast
from uuid import uuid4

from starlette.testclient import TestClient

from serena_light.daemon.server import DaemonService, create_daemon_app
from serena_light.lsp.positions import FileSnapshot, PositionEncoding
from serena_light.runtime_files import BearerSecret
from serena_light.tools.envelopes import WorkspaceMetadata
from serena_light.tools.navigation import DocumentNavigation, DocumentSymbolInput, find_symbol


class _Service:
    def __init__(self) -> None:
        self.lease_id = str(uuid4())
        self.semantic_calls: list[tuple[str, Mapping[str, object]]] = []

    async def status(self, *, mcp_session_id: str) -> Mapping[str, object]:
        return {"session": mcp_session_id}

    async def migration_status(self) -> Mapping[str, object]:
        return {"active_holders": 1, "daemon_idle": False}

    async def acquire_lease(self, *, mcp_session_id: str) -> Mapping[str, object]:
        return {"lease_id": self.lease_id, "daemon_id": "ignored", "session": mcp_session_id}

    async def heartbeat(self, *, lease_id: str) -> Mapping[str, object]:
        return {"lease_id": lease_id, "active": True}

    async def release_lease(self, *, lease_id: str, immediate: bool) -> Mapping[str, object]:
        return {"lease_id": lease_id, "released": True, "immediate": immediate}

    async def activate_workspace(self, *, lease_id: str, absolute_path: str) -> Mapping[str, object]:
        return {"lease_id": lease_id, "workspace": absolute_path}

    async def release_workspace(self, *, lease_id: str, immediate: bool = False) -> Mapping[str, object]:
        return {"lease_id": lease_id, "released": True, "immediate": immediate}

    async def get_runtime_status(self, *, lease_id: str) -> Mapping[str, object]:
        return {"lease_id": lease_id, "authority": "native", "generation": 8}

    async def semantic_operation(
        self, *, lease_id: str, operation: str, **kwargs: object
    ) -> Mapping[str, object]:
        del lease_id
        self.semantic_calls.append((operation, dict(kwargs)))
        workspace = {"root": "/repo", "kind": "git", "working_subdirectory": "/repo"}
        if operation == "find_symbol":
            if kwargs.get("name_path") == "ambiguous":
                return _ambiguous_symbol_result(
                    max_answer_chars=cast(int, kwargs["max_answer_chars"]),
                    error_max_answer_chars=cast(int, kwargs["_error_max_answer_chars"]),
                )
            if str(kwargs.get("name_path", "")).startswith("missing"):
                return {
                    "ok": False,
                    "error": {
                        "code": "SYMBOL_NOT_FOUND",
                        "message": "symbol was not found",
                        "details": {
                            "relative_path": kwargs.get("relative_path"),
                            "name_path": kwargs.get("name_path"),
                            "engine": {
                                "adapter": "pyright",
                                "interpreter": "/service/python",
                            },
                        },
                    },
                    "workspace": workspace,
                    "adapter": {"name": "pyright", "language": "python"},
                    "generations": {"trust": 1, "program": 2, "document": 3, "index": 4},
                }
            return {
                "ok": True,
                "data": {
                    "relative_path": "src/main.py",
                    "sha256": "a" * 64,
                    "symbol": {
                        "name": "answer",
                        "name_path": "answer",
                        "kind": 13,
                        "range": {
                            "start": {"line": 0, "column": 0, "text_offset": 0, "byte_offset": 0},
                            "end": {"line": 0, "column": 6, "text_offset": 6, "byte_offset": 6},
                        },
                    },
                },
                "workspace": workspace,
                "adapter": {"name": "pyright", "language": "python"},
                "generations": {"trust": 1, "program": 2, "document": 3, "index": 4},
                "truncation": {"truncated": False, "omitted_count": 0},
            }
        if operation == "find_declaration" and str(kwargs.get("regex", "")).startswith(
            "(def)"
        ):
            return {
                "ok": False,
                "error": {
                    "code": "AMBIGUOUS_SYMBOL",
                    "message": "symbol selection is ambiguous",
                    "details": {
                        "relative_path": kwargs.get("relative_path"),
                        "regex": kwargs.get("regex"),
                        "occurrence_count": 12,
                    },
                },
                "workspace": workspace,
                "adapter": {"name": "pyright", "language": "python"},
                "generations": {"trust": 1, "program": 2, "document": 3, "index": 4},
            }
        if operation == "get_symbols_overview":
            return {
                "ok": True,
                "data": {
                    "relative_path": "src/main.py",
                    "symbols": [
                        {"name": "Runner", "kind": 5, "children": []},
                        {"name": "run", "kind": 6, "children": []},
                    ],
                },
                "workspace": workspace,
                "adapter": {"name": "pyright", "language": "python"},
                "generations": {"trust": 1, "program": 2, "document": 3, "index": 4},
                "truncation": {"truncated": False, "omitted_count": 0},
            }
        if operation == "find_implementations":
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
            return {
                "ok": True,
                "data": {
                    "relative_path": "src/main.py",
                    "name_path": "Runner",
                    "locations": [
                        location
                        for location in locations
                        if (not included or location["kind"] in included)
                        and location["kind"] not in excluded
                    ],
                },
                "workspace": workspace,
                "adapter": {"name": "pyright", "language": "python"},
                "generations": {"trust": 1, "program": 2, "document": 3, "index": 4},
                "truncation": {"truncated": False, "omitted_count": 0},
            }
        if operation in {"get_diagnostics_for_file", "get_diagnostics_for_symbol"}:
            if operation == "get_diagnostics_for_symbol" and kwargs.get("name_path") == "ambiguous":
                return {
                    "ok": False,
                    "error": {
                        "code": "AMBIGUOUS_SYMBOL",
                        "message": "symbol selection is ambiguous",
                        "retry": None,
                        "details": {
                            "relative_path": "src/main.py",
                            "name_path": "ambiguous",
                            "engine": "pyright",
                            "interpreter": "/service/python",
                            "candidates": [f"Container{index:03d}/ambiguous" for index in range(80)],
                        },
                    },
                    "workspace": workspace,
                    "adapter": {"name": "pyright", "language": "python"},
                    "generations": {"trust": 1, "program": 2, "document": 3, "index": 4},
                }
            count = 1 if kwargs.get("relative_path") == "src/long.py" else 12
            findings = [
                {
                    "severity": "error",
                    "range": {
                        "start": {
                            "line": index,
                            "column": 0,
                            "text_offset": index * 8,
                            "byte_offset": index * 8,
                        },
                        "end": {
                            "line": index,
                            "column": 5,
                            "text_offset": index * 8 + 5,
                            "byte_offset": index * 8 + 5,
                        },
                    },
                    "message": (
                        "Argument of type X is not assignable to parameter of type Y. " * 8
                        if count == 1
                        else f"current diagnostic {index}"
                    ),
                    "source": "pyright",
                    "code": f"report-{index}",
                }
                for index in range(count)
            ]
            name_path = "answer" if operation == "get_diagnostics_for_symbol" else "<file>"
            return {
                "ok": True,
                "data": {
                    "state": "findings",
                    "relative_path": "src/main.py",
                    "sha256": "a" * 64,
                    "diagnostics_generation": 3,
                    "engine": {
                        "adapter": "pyright",
                        "language": "python",
                        "authority": "native",
                        "interpreter": "/service/python",
                    },
                    "groups": [{"name_path": name_path, "findings": findings}],
                },
                "workspace": workspace,
                "adapter": {"name": "pyright", "language": "python"},
                "generations": {"trust": 1, "program": 2, "document": 3, "index": 4},
                "truncation": {
                    "truncated": False,
                    "omitted_count": (
                        "bad" if kwargs.get("relative_path") == "src/malformed.py" else 0
                    ),
                },
            }
        return {
            "ok": False,
            "error": {
                "code": "NOT_READY",
                "message": "semantic service is not ready",
                "retry": {"retryable": True, "target_generation": 9},
                "details": {"tool": operation},
            },
            "workspace": workspace,
            "adapter": {"name": "pyright", "language": "python", "phase": "cold"},
            "generations": {"trust": 1, "program": 2, "document": 3, "index": 4},
        }


def _ambiguous_symbol_result(*, max_answer_chars: int, error_max_answer_chars: int) -> Mapping[str, object]:
    names = [f"ambiguous_candidate_{index:03d}_{'x' * 48}" for index in range(80)]
    snapshot = FileSnapshot.from_bytes("".join(f"{name}\n" for name in names).encode())
    document = DocumentNavigation.from_input(
        DocumentSymbolInput(
            "src/ambiguous.py",
            "file:///repo/src/ambiguous.py",
            snapshot,
            [
                {
                    "name": name,
                    "kind": 12,
                    "range": {
                        "start": {"line": index, "character": 0},
                        "end": {"line": index, "character": len(name)},
                    },
                    "selectionRange": {
                        "start": {"line": index, "character": 0},
                        "end": {"line": index, "character": len(name)},
                    },
                }
                for index, name in enumerate(names)
            ],
            PositionEncoding.UTF16,
            WorkspaceMetadata("/repo", "git", "/repo"),
        )
    )
    return find_symbol(
        document,
        "ambiguous",
        substring_matching=True,
        max_answer_chars=max_answer_chars,
        _error_max_answer_chars=error_max_answer_chars,
    ).to_dict()


def _initialize(client: TestClient, authorization: str) -> str:
    response = client.post(
        "/mcp",
        headers={
            "Authorization": authorization,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "compact-test", "version": "1"},
            },
        },
    )
    assert response.status_code == 200
    return response.headers["mcp-session-id"]


def _rpc(
    client: TestClient,
    authorization: str,
    session_id: str,
    request_id: int,
    method: str,
    params: Mapping[str, object],
) -> dict[str, Any]:
    response = client.post(
        "/mcp",
        headers={
            "Authorization": authorization,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Mcp-Session-Id": session_id,
            "Mcp-Protocol-Version": "2025-11-25",
        },
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)},
    )
    assert response.status_code == 200, response.text
    return cast(dict[str, Any], response.json()["result"])


def _tool_call(
    client: TestClient,
    authorization: str,
    session_id: str,
    request_id: int,
    name: str,
    arguments: Mapping[str, object],
    lease_id: str,
) -> dict[str, Any]:
    return _rpc(
        client,
        authorization,
        session_id,
        request_id,
        "tools/call",
        {
            "name": name,
            "arguments": dict(arguments),
            "_meta": {"serena_light": {"lease_id": lease_id}},
        },
    )


def test_real_fastmcp_boundary_emits_exact_compact_text_and_public_schema() -> None:
    service = _Service()
    token = "z" * 48
    authorization = f"Bearer {token}"
    app = create_daemon_app(
        service=cast(DaemonService, service),
        bearer=BearerSecret(token),
        daemon_id=str(uuid4()),
    )

    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50100)) as client:
        session_id = _initialize(client, authorization)
        listed = _rpc(client, authorization, session_id, 2, "tools/list", {})
        tools = {tool["name"]: tool for tool in listed["tools"]}
        symbol_properties = tools["find_symbol"]["inputSchema"]["properties"]
        overview_properties = tools["get_symbols_overview"]["inputSchema"]["properties"]
        declaration_properties = tools["find_declaration"]["inputSchema"]["properties"]
        diagnostic_properties = tools["get_diagnostics_for_symbol"]["inputSchema"]["properties"]
        assert "max_matches" in symbol_properties
        assert {"include_kinds", "exclude_kinds"} <= overview_properties.keys()
        assert "max_answer_chars" in declaration_properties
        assert "compact" not in symbol_properties
        assert "max_candidates_per_adapter" not in symbol_properties
        assert "1 through 100" in symbol_properties["max_matches"]["description"]
        assert "default 20" in symbol_properties["max_matches"]["description"]
        assert "prefix /" in symbol_properties["name_path"]["description"]
        assert "file or directory" in symbol_properties["relative_path"]["description"]
        assert "512 through 50000" in symbol_properties["max_answer_chars"]["description"]
        assert "variable/constant" in overview_properties["include_kinds"]["description"]
        assert "lowercase LSP kind names" in overview_properties["exclude_kinds"]["description"]
        assert "512 through 50000" in declaration_properties["max_answer_chars"]["description"]
        assert "1=Error" in diagnostic_properties["maximum_severity"]["description"]
        assert "512 through 50000" in diagnostic_properties["max_answer_chars"]["description"]
        assert not ({"find_files", "search_for_pattern", "notify_file_changed"} & tools.keys())

        result = _tool_call(
            client,
            authorization,
            session_id,
            3,
            "find_symbol",
            {
                "name_path": "answer",
                "relative_path": "src/main.py",
                "max_answer_chars": 512,
                "max_matches": 1,
            },
            service.lease_id,
        )
        text = result["content"][0]["text"]
        structured = result["structuredContent"]
        assert text == json.dumps(structured, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        assert len(text) <= 512
        assert structured == {
            "ok": True,
            "data": {
                "workspace": "/repo",
                "files": [
                    {
                        "path": "src/main.py",
                        "symbols": [{"name_path": "answer", "kind": "variable", "range": [[0, 0], [0, 6]]}],
                    }
                ],
                "omitted": 0,
            },
        }
        assert service.semantic_calls[0][1]["max_answer_chars"] == 2_147_483_647
        assert "max_matches" not in service.semantic_calls[0][1]


def test_real_fastmcp_diagnostics_enforces_budget_only_at_final_boundary() -> None:
    service = _Service()
    token = "d" * 48
    authorization = f"Bearer {token}"
    app = create_daemon_app(
        service=cast(DaemonService, service),
        bearer=BearerSecret(token),
        daemon_id=str(uuid4()),
    )

    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50104)) as client:
        session_id = _initialize(client, authorization)
        for request_id, operation, arguments in (
            (
                2,
                "get_diagnostics_for_file",
                {"relative_path": "src/main.py", "max_answer_chars": 512},
            ),
            (
                3,
                "get_diagnostics_for_symbol",
                {
                    "relative_path": "src/main.py",
                    "name_path": "answer",
                    "max_answer_chars": 512,
                },
            ),
        ):
            result = _tool_call(
                client,
                authorization,
                session_id,
                request_id,
                operation,
                arguments,
                service.lease_id,
            )
            text = result["content"][0]["text"]
            structured = result["structuredContent"]
            assert text == json.dumps(
                structured,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            assert len(text) <= 512
            assert structured["ok"] is True
            data = structured["data"]
            assert data["workspace"] == "/repo"
            assert data["files"][0]["path"] == "src/main.py"
            assert 0 < len(data["files"][0]["diagnostics"]) < 12
            assert data["omitted"] == 12 - len(data["files"][0]["diagnostics"])
            assert not (
                {
                    "sha256",
                    "generations",
                    "adapter",
                    "engine",
                    "interpreter",
                    "diagnostics_generation",
                }
                & set(text.split('"'))
            )

    assert [call[0] for call in service.semantic_calls] == [
        "get_diagnostics_for_file",
        "get_diagnostics_for_symbol",
    ]
    assert all(
        call[1]["max_answer_chars"] == 2_147_483_647 for call in service.semantic_calls
    )


def test_real_fastmcp_diagnostics_rejects_a_first_finding_that_cannot_fit() -> None:
    service = _Service()
    token = "w" * 48
    authorization = f"Bearer {token}"
    app = create_daemon_app(
        service=cast(DaemonService, service),
        bearer=BearerSecret(token),
        daemon_id=str(uuid4()),
    )

    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50105)) as client:
        session_id = _initialize(client, authorization)
        results = [
            _tool_call(
                client,
                authorization,
                session_id,
                request_id,
                tool,
                {
                    "relative_path": "src/long.py",
                    **({"name_path": "answer"} if tool.endswith("_for_symbol") else {}),
                    "max_answer_chars": 512,
                },
                service.lease_id,
            )
            for request_id, tool in enumerate(
                ("get_diagnostics_for_file", "get_diagnostics_for_symbol"),
                start=2,
            )
        ]

    for result in results:
        text = result["content"][0]["text"]
        payload = result["structuredContent"]
        assert len(text) <= 512
        assert text == json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        assert payload["ok"] is False
        assert payload["error"]["code"] == "INVALID_INPUT"
        details = payload["error"]["details"]
        assert details["field"] == "max_answer_chars"
        assert details["minimum_required_chars"] > 512
    assert all(
        call[1]["max_answer_chars"] == 2_147_483_647 for call in service.semantic_calls
    )


def test_real_fastmcp_diagnostics_types_malformed_internal_truncation() -> None:
    service = _Service()
    token = "w" * 48
    authorization = f"Bearer {token}"
    app = create_daemon_app(
        service=cast(DaemonService, service),
        bearer=BearerSecret(token),
        daemon_id=str(uuid4()),
    )

    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50106)) as client:
        session_id = _initialize(client, authorization)
        results = [
            _tool_call(
                client,
                authorization,
                session_id,
                request_id,
                tool,
                {
                    "relative_path": "src/malformed.py",
                    **({"name_path": "answer"} if tool.endswith("_for_symbol") else {}),
                    "max_answer_chars": 512,
                },
                service.lease_id,
            )
            for request_id, tool in enumerate(
                ("get_diagnostics_for_file", "get_diagnostics_for_symbol"),
                start=2,
            )
        ]

    for result in results:
        assert result.get("isError") is not True
        payload = result["structuredContent"]
        assert payload["ok"] is False
        assert payload["error"]["code"] == "UNSUPPORTED"
        assert payload["error"]["details"]["reason"] == "malformed_diagnostics_success"


def test_real_fastmcp_boundary_keeps_ambiguous_candidate_errors_at_public_budget() -> None:
    service = _Service()
    token = "w" * 48
    authorization = f"Bearer {token}"
    app = create_daemon_app(
        service=cast(DaemonService, service),
        bearer=BearerSecret(token),
        daemon_id=str(uuid4()),
    )

    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50103)) as client:
        session_id = _initialize(client, authorization)
        narrow = _tool_call(
            client,
            authorization,
            session_id,
            2,
            "find_symbol",
            {
                "name_path": "ambiguous",
                "relative_path": "src/ambiguous.py",
                "substring_matching": True,
                "max_answer_chars": 512,
            },
            service.lease_id,
        )["structuredContent"]
        wide = _tool_call(
            client,
            authorization,
            session_id,
            3,
            "find_symbol",
            {
                "name_path": "ambiguous",
                "relative_path": "src/ambiguous.py",
                "substring_matching": True,
                "max_answer_chars": 12_000,
            },
            service.lease_id,
        )["structuredContent"]
        diagnostic = _tool_call(
            client,
            authorization,
            session_id,
            4,
            "get_diagnostics_for_symbol",
            {
                "relative_path": "src/main.py",
                "name_path": "ambiguous",
                "max_answer_chars": 512,
            },
            service.lease_id,
        )

    narrow_details = narrow["error"]["details"]
    wide_details = wide["error"]["details"]
    assert narrow["error"]["code"] == "AMBIGUOUS_SYMBOL"
    assert narrow_details["truncated"] is True
    assert narrow_details["omitted_count"] > 0
    assert len(narrow_details["candidates"]) < len(wide_details["candidates"])
    assert service.semantic_calls[0][1]["max_answer_chars"] == 2_147_483_647
    assert service.semantic_calls[0][1]["_error_max_answer_chars"] == 512
    assert service.semantic_calls[1][1]["max_answer_chars"] == 2_147_483_647
    assert service.semantic_calls[1][1]["_error_max_answer_chars"] == 12_000
    diagnostic_text = diagnostic["content"][0]["text"]
    diagnostic_payload = diagnostic["structuredContent"]
    assert len(diagnostic_text) <= 512
    assert diagnostic_text == json.dumps(
        diagnostic_payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    assert diagnostic_payload["error"]["code"] == "AMBIGUOUS_SYMBOL"
    diagnostic_details = diagnostic_payload["error"]["details"]
    assert diagnostic_details["truncated"] is True
    assert diagnostic_details["omitted_count"] > 0
    assert "adapter" not in diagnostic_payload and "generations" not in diagnostic_payload
    assert service.semantic_calls[2][1]["max_answer_chars"] == 2_147_483_647


def test_real_fastmcp_boundary_bounds_long_deterministic_and_candidate_free_errors() -> None:
    service = _Service()
    token = "w" * 48
    authorization = f"Bearer {token}"
    app = create_daemon_app(
        service=cast(DaemonService, service),
        bearer=BearerSecret(token),
        daemon_id=str(uuid4()),
    )

    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50104)) as client:
        session_id = _initialize(client, authorization)
        missing = _tool_call(
            client,
            authorization,
            session_id,
            2,
            "find_symbol",
            {
                "name_path": "missing" * 200,
                "relative_path": "src/main.py",
                "max_answer_chars": 512,
            },
            service.lease_id,
        )
        occurrence_ambiguity = _tool_call(
            client,
            authorization,
            session_id,
            3,
            "find_declaration",
            {
                "relative_path": "src/main.py",
                "regex": "(def)" + "(?:)" * 300,
                "max_answer_chars": 512,
            },
            service.lease_id,
        )

    for result, code in (
        (missing, "SYMBOL_NOT_FOUND"),
        (occurrence_ambiguity, "AMBIGUOUS_SYMBOL"),
    ):
        text = result["content"][0]["text"]
        payload = result["structuredContent"]
        assert len(text) <= 512
        assert text == json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        assert payload["error"]["code"] == code
        assert "adapter" not in payload and "generations" not in payload

    assert "engine" not in missing["structuredContent"]["error"]["details"]
    ambiguity_details = occurrence_ambiguity["structuredContent"]["error"]["details"]
    assert ambiguity_details["occurrence_count"] == 12
    assert "regex" not in ambiguity_details


def test_invalid_public_limit_fails_before_semantic_dispatch_and_rich_errors_stay_rich() -> None:
    service = _Service()
    token = "y" * 48
    authorization = f"Bearer {token}"
    app = create_daemon_app(
        service=cast(DaemonService, service),
        bearer=BearerSecret(token),
        daemon_id=str(uuid4()),
    )

    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50101)) as client:
        session_id = _initialize(client, authorization)
        invalid = _tool_call(
            client,
            authorization,
            session_id,
            2,
            "find_symbol",
            {"name_path": "answer", "max_matches": 0},
            service.lease_id,
        )["structuredContent"]
        assert invalid["ok"] is False
        assert invalid["error"]["code"] == "INVALID_INPUT"
        assert invalid["error"]["details"]["field"] == "max_matches"
        assert service.semantic_calls == []

        invalid_implementation_filter = _tool_call(
            client,
            authorization,
            session_id,
            3,
            "find_implementations",
            {"name_path": "Runner", "relative_path": "src/main.py", "include_kinds": [27]},
            service.lease_id,
        )["structuredContent"]
        assert invalid_implementation_filter["ok"] is False
        assert invalid_implementation_filter["error"]["code"] == "INVALID_INPUT"
        assert invalid_implementation_filter["error"]["details"]["field"] == "include_kinds or exclude_kinds"
        assert service.semantic_calls == []

        invalid_budget_cases = (
            ("get_symbols_overview", {"relative_path": "src/main.py", "max_answer_chars": 10}),
            ("find_symbol", {"name_path": "answer", "max_answer_chars": 60_000}),
            (
                "find_referencing_symbols",
                {"relative_path": "src/main.py", "name_path": "answer", "max_answer_chars": 100},
            ),
            (
                "find_declaration",
                {"relative_path": "src/main.py", "regex": "(answer)", "max_answer_chars": 100},
            ),
            (
                "find_implementations",
                {"relative_path": "src/main.py", "name_path": "Runner", "max_answer_chars": 100},
            ),
        )
        for request_id, (tool, arguments) in enumerate(invalid_budget_cases, start=4):
            result = _tool_call(
                client,
                authorization,
                session_id,
                request_id,
                tool,
                arguments,
                service.lease_id,
            )
            assert result.get("isError") is not True
            payload = result["structuredContent"]
            assert payload["ok"] is False
            assert payload["error"]["code"] == "INVALID_INPUT"
            assert payload["error"]["details"]["field"] == "max_answer_chars"
        assert service.semantic_calls == []

        rich = _tool_call(
            client,
            authorization,
            session_id,
            20,
            "find_declaration",
            {"relative_path": "src/main.py", "regex": "(answer)"},
            service.lease_id,
        )["structuredContent"]
        assert rich["ok"] is False
        assert rich["error"]["code"] == "NOT_READY"
        assert rich["error"]["retry"] == {"retryable": True, "target_generation": 9}
        assert rich["adapter"]["phase"] == "cold"
        assert rich["generations"]["document"] == 3


def test_kind_filters_keep_compact_and_semantic_ownership_separate() -> None:
    service = _Service()
    token = "x" * 48
    authorization = f"Bearer {token}"
    app = create_daemon_app(
        service=cast(DaemonService, service),
        bearer=BearerSecret(token),
        daemon_id=str(uuid4()),
    )

    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50102)) as client:
        session_id = _initialize(client, authorization)
        overview = _tool_call(
            client,
            authorization,
            session_id,
            2,
            "get_symbols_overview",
            {"relative_path": "src/main.py", "include_kinds": ["class"]},
            service.lease_id,
        )["structuredContent"]
        assert overview["ok"] is True
        assert service.semantic_calls[-1] == (
            "get_symbols_overview",
            {"relative_path": "src/main.py", "max_depth": 0, "max_answer_chars": 2_147_483_647},
        )

        implementations = _tool_call(
            client,
            authorization,
            session_id,
            3,
            "find_implementations",
            {
                "name_path": "Runner",
                "relative_path": "src/main.py",
                "include_kinds": [5, 6],
                "exclude_kinds": [5],
            },
            service.lease_id,
        )["structuredContent"]
        assert implementations == {
            "ok": True,
            "data": {
                "workspace": "/repo",
                "files": [
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
                ],
                "omitted": 0,
            },
        }
        assert service.semantic_calls[-1] == (
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
