"""Cross-boundary connector contract acceptance for tasks 12.7 and 15.2.

Every case below drives the stack an agent actually uses: a real
:class:`Connector` over authenticated loopback Streamable HTTP, the shipped
daemon application, a real :class:`WorkspaceDaemonService`, lease lifecycle and
runtime registry, and a real :class:`WorkspaceRuntime` on a real Git workspace.
Only the language server itself is a deterministic fake, so every freshness,
authorization, timeout, and envelope decision asserted here is made by
production code rather than by a test double.

Two faults are injected deliberately and only where production owns no seam a
test could otherwise reach: ``os.fsync`` failing on directories, and one dropped
``tools/call`` response at the public :class:`DaemonSession` protocol seam.

The freshness races at the end of this file add a third real participant: a
separate, non-cooperating writer process that rewrites workspace files at
explicit barriers while one read attempt already owns its snapshot.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import socket
import stat
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from concurrent.futures import Future
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import UUID, uuid4

import anyio
import pytest
import uvicorn
from mcp import ClientSession, types
from mcp.server import Server
from mcp.shared.message import SessionMessage
from starlette.types import ASGIApp

from serena_light import __version__
from serena_light import connector as connector_module
from serena_light.connector import (
    Connector,
    ConnectorSessionLost,
    DaemonEndpoint,
    DaemonSession,
    LeaseGrant,
    McpSessionFactory,
    SessionFactory,
    build_proxy_server,
)
from serena_light.daemon.leases import LeaseLifecycle
from serena_light.daemon.server import LOOPBACK_HOST, MCP_PATH, DaemonService, create_daemon_app
from serena_light.daemon.service import WorkspaceDaemonService
from serena_light.lsp.adapter import (
    AdapterGenerations,
    AdapterPhase,
    AdapterSnapshot,
    CrashSnapshot,
    DerivedToolAvailability,
    DocumentReadinessProbe,
    DocumentReadinessTarget,
    EngineMetadata,
    RawLspProviders,
)
from serena_light.lsp.executor import BoundedLspExecutor
from serena_light.lsp.positions import FileSnapshot, PositionEncoding
from serena_light.lsp.state import DiagnosticsSnapshot, DiagnosticsState
from serena_light.runtime_files import LEGACY_BUILD_IDENTITY, BearerSecret
from serena_light.tools.editing import NotificationResult, ReplacementNotification
from serena_light.workspace.identity import (
    WorkspaceIdentity,
    WorkspaceKind,
    WorkspacePolicy,
)
from serena_light.workspace.registry import WorkspaceRuntimeRegistry
from serena_light.workspace.runtime import AdapterBuildContext, AdapterFactory, WorkspaceRuntime
from serena_light.workspace.scope import (
    LanguageFamily,
    NativeProgramAttribution,
    ProjectKind,
    ScopeProjection,
)

_SOURCE = b"def target():\n    return 1\n"
_NEW_BODY = "def target():\n    return 2"
_SOURCE_HASH = hashlib.sha256(_SOURCE).hexdigest()

# Heartbeats are irrelevant to these contracts; keep the loop from ever firing.
_QUIET_HEARTBEAT_SECONDS = 3600.0
_SERVER_TIMEOUT_SECONDS = 10.0

_EXTENSIONS: Mapping[LanguageFamily, frozenset[str]] = {
    LanguageFamily.PYTHON: frozenset({".py", ".pyi"}),
    LanguageFamily.TYPESCRIPT: frozenset({".ts", ".tsx"}),
}

# Production refuses Python diagnostics from any server other than the pinned
# Pyright, so the stand-in reports the engine name its family really uses.
_ENGINE_NAMES: Mapping[LanguageFamily, str] = {
    LanguageFamily.PYTHON: "pyright",
    LanguageFamily.TYPESCRIPT: "typescript-language-server",
}

_SYMBOLS: tuple[Mapping[str, Any], ...] = (
    {
        "name": "target",
        "kind": 12,
        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 1, "character": 12}},
        "selectionRange": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 10}},
    },
)

_WORKSPACE_TOOLS = frozenset(
    {
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
    }
)


def _requested_uri(params: object) -> str:
    """The document one request names, or empty for a request that names none."""

    if not isinstance(params, Mapping):
        return ""
    document = cast(Mapping[str, object], params).get("textDocument")
    if not isinstance(document, Mapping):
        return ""
    uri = cast(Mapping[str, object], document).get("uri")
    return uri if isinstance(uri, str) else ""


class _Client:
    """Deterministic stand-in for one language-server client connection."""

    def __init__(self) -> None:
        self.before_request: Callable[[], None] | None = None
        # Fires once a request has produced its answer, so a race scenario can
        # act exactly when one attempt already owns that answer.
        self.after_request: Callable[[str, str], None] | None = None
        self.requests: list[str] = []
        self.notifications: list[str] = []
        self.document_symbols: tuple[Mapping[str, Any], ...] = _SYMBOLS
        # When set, document symbols answer the named document instead of one
        # fixed reply, the way a server that analyzed current bytes would.
        self.analyze: Callable[[str], tuple[Mapping[str, Any], ...]] | None = None
        self.selection_ranges: tuple[Mapping[str, Any], ...] = ()
        self.references: tuple[Mapping[str, Any], ...] = ()

    def request(self, method: str, params: object = None, *, timeout: float | None = None) -> object:
        del timeout
        self.requests.append(method)
        if self.before_request is not None:
            self.before_request()
        uri = _requested_uri(params)
        if method == "textDocument/documentSymbol":
            answer = list(self.document_symbols if self.analyze is None else self.analyze(uri))
        elif method == "textDocument/selectionRange":
            answer = list(self.selection_ranges)
        elif method == "textDocument/references":
            answer = list(self.references)
        else:
            answer = list(_SYMBOLS)
        if self.after_request is not None:
            self.after_request(method, uri)
        return answer

    def notify(self, method: str, params: object = None) -> None:
        del params
        self.notifications.append(method)

    def shutdown(self, *, timeout: float = 2.0) -> None:
        del timeout


class _Adapter:
    """A language-server stand-in; every workspace decision stays in production code."""

    def __init__(self, context: AdapterBuildContext) -> None:
        self.context = context
        self.client = _Client()
        self.before_edit: Callable[[], None] | None = None
        self.document_generation = 0
        # The exact text every document generation analyzed, so a publication
        # belongs to the bytes that produced it rather than to current disk.
        self.analyzed: dict[int, str] = {}
        self.diagnose: Callable[[str], tuple[Mapping[str, Any], ...]] | None = None

    def routes(self, path: str | Path) -> bool:
        return PurePosixPath(str(path)).suffix.lower() in _EXTENSIONS[self.context.family]

    def snapshot(self) -> AdapterSnapshot:
        raw = RawLspProviders(
            definition=True,
            implementation=False,
            references=True,
            document_symbols=True,
            workspace_symbols=True,
        )
        return AdapterSnapshot(
            self.context.family.value,
            AdapterPhase.READY,
            raw,
            DerivedToolAvailability.from_raw(raw),
            EngineMetadata(_ENGINE_NAMES[self.context.family], "1.0", Path("/owned/server"), Path("/owned/python")),
            PositionEncoding.UTF16,
            AdapterGenerations(1, 2, self.document_generation, 3),
            CrashSnapshot(0, 0, None, None, None, 0.0),
            (),
            True,
        )

    def snapshot_open_and_probe_document(
        self,
        *,
        absolute_path: Path,
        relative_path: str,
        uri: str,
        version: int,
        probe: DocumentReadinessProbe,
    ) -> Future[tuple[FileSnapshot, DocumentReadinessTarget]]:
        def worker() -> tuple[FileSnapshot, DocumentReadinessTarget]:
            # Take the snapshot before the probe, as a server that opens a
            # document and then answers about that opened text does.
            snapshot = FileSnapshot.from_bytes(absolute_path.read_bytes())
            self.document_generation += 1
            target = DocumentReadinessTarget(uri, relative_path, absolute_path, version, self.document_generation, 0)
            self.analyzed[target.document_generation] = snapshot.text
            assert probe.observe(self.client, target, timeout=1.0)
            return snapshot, target

        return self.context.executor.submit(worker)

    def diagnostics_snapshot(self, target: DocumentReadinessTarget) -> DiagnosticsSnapshot:
        """Publish diagnostics for the text this document generation analyzed."""

        analyzed = self.analyzed.get(target.document_generation)
        if self.diagnose is None or analyzed is None:
            return DiagnosticsSnapshot(
                DiagnosticsState.MISSING,
                target.uri,
                target.absolute_path,
                target.version,
                target.document_generation,
                None,
                (),
            )
        diagnostics = self.diagnose(analyzed)
        return DiagnosticsSnapshot(
            DiagnosticsState.FINDINGS if diagnostics else DiagnosticsState.CLEAN,
            target.uri,
            target.absolute_path,
            target.version,
            target.document_generation,
            target.document_generation,
            diagnostics,
        )

    def submit_read(self, operation: Callable[[_Client], Any]) -> Future[Any]:
        return self.context.executor.submit(lambda: operation(self.client))

    def submit_edit(self, operation: Callable[[_Client], Any]) -> Future[Any]:
        def worker() -> Any:
            # Runs on the edit worker, after authorization and freshness but
            # before the transaction takes the workspace lock.
            if self.before_edit is not None:
                self.before_edit()
            return operation(self.client)

        return self.context.executor.submit(worker)

    def open_snapshot_document_with_client(
        self,
        client: _Client,
        *,
        absolute_path: Path,
        relative_path: str,
        uri: str,
        version: int,
        text: str,
    ) -> DocumentReadinessTarget:
        """Open one response-owned semantic target on the caller's own client."""

        del client
        self.document_generation += 1
        self.analyzed[self.document_generation] = text
        return DocumentReadinessTarget(uri, relative_path, absolute_path, version, self.document_generation, 0)

    def open_edit_document_with_client(
        self,
        client: _Client,
        *,
        absolute_path: Path,
        relative_path: str,
        uri: str,
        version: int,
        text: str,
    ) -> DocumentReadinessTarget:
        del client, text
        self.document_generation += 1
        return DocumentReadinessTarget(uri, relative_path, absolute_path, version, self.document_generation, 0)

    def notify_edit_with_client(
        self,
        client: _Client,
        target: DocumentReadinessTarget,
        notification: ReplacementNotification,
    ) -> NotificationResult:
        del target, notification
        client.notify("textDocument/didChange")
        self.document_generation += 1
        return NotificationResult("notified", self.document_generation)

    def stop(self) -> Future[AdapterSnapshot]:
        return self.context.executor.submit(self.snapshot)


class _ObservedService:
    """Delegate the whole daemon seam while recording semantic invocations.

    The recording exists so a lost response can be proven to have reached the
    daemon exactly once; it adds no behaviour of its own.
    """

    def __init__(
        self,
        service: WorkspaceDaemonService[
            tuple[WorkspaceKind, Path, str, Path],
            WorkspaceRuntime,
        ],
        semantic_calls: list[str],
    ) -> None:
        self._service = service
        self._semantic_calls = semantic_calls

    async def status(self, *, mcp_session_id: str) -> Mapping[str, object]:
        return await self._service.status(mcp_session_id=mcp_session_id)

    async def acquire_lease(self, *, mcp_session_id: str) -> Mapping[str, object]:
        return await self._service.acquire_lease(mcp_session_id=mcp_session_id)

    async def heartbeat(self, *, lease_id: str) -> Mapping[str, object]:
        return await self._service.heartbeat(lease_id=lease_id)

    async def release_lease(self, *, lease_id: str, immediate: bool) -> Mapping[str, object]:
        return await self._service.release_lease(lease_id=lease_id, immediate=immediate)

    async def activate_workspace(
        self,
        *,
        lease_id: str,
        absolute_path: str,
        python_environment: str | None = None,
    ) -> Mapping[str, object]:
        return await self._service.activate_workspace(
            lease_id=lease_id,
            absolute_path=absolute_path,
            python_environment=python_environment,
        )

    async def release_workspace(self, *, lease_id: str, immediate: bool = False) -> Mapping[str, object]:
        return await self._service.release_workspace(lease_id=lease_id, immediate=immediate)

    async def get_runtime_status(self, *, lease_id: str) -> Mapping[str, object]:
        return await self._service.get_runtime_status(lease_id=lease_id)

    async def semantic_operation(self, *, lease_id: str, operation: str, **kwargs: object) -> Mapping[str, object]:
        self._semantic_calls.append(operation)
        return await self._service.semantic_operation(lease_id=lease_id, operation=operation, **kwargs)


class _StaticDiscovery:
    """Already validated discovery for a daemon this test process owns."""

    def __init__(self, endpoint: DaemonEndpoint) -> None:
        self._endpoint = endpoint

    async def discover(self) -> DaemonEndpoint:
        return self._endpoint


class _LostResponseSession:
    """Forward everything, but drop exactly one already-executed tool response."""

    def __init__(self, inner: DaemonSession, owner: _LostResponseSessionFactory) -> None:
        self._inner = inner
        self._owner = owner

    async def acquire_lease(self) -> LeaseGrant:
        return await self._inner.acquire_lease()

    async def heartbeat(self, lease_id: str) -> None:
        await self._inner.heartbeat(lease_id)

    async def release_lease(self, lease_id: str) -> None:
        await self._inner.release_lease(lease_id)

    async def activate_workspace(
        self,
        lease_id: str,
        path: Path,
        python_environment: str | None = None,
    ) -> types.CallToolResult:
        return await self._inner.activate_workspace(lease_id, path, python_environment)

    async def list_tools(self) -> types.ListToolsResult:
        return await self._inner.list_tools()

    async def call_tool(self, lease_id: str, name: str, arguments: Mapping[str, object] | None) -> types.CallToolResult:
        result = await self._inner.call_tool(lease_id, name, arguments)
        if name == self._owner.drop_tool and not self._owner.dropped:
            self._owner.dropped = True
            raise ConnectorSessionLost("simulated lost tools/call response")
        return result

    async def aclose(self) -> None:
        await self._inner.aclose()


class _LostResponseSessionFactory:
    """Real sessions whose first response for one tool never reaches the caller."""

    def __init__(self, drop_tool: str) -> None:
        self.drop_tool = drop_tool
        self.dropped = False
        self.connects = 0
        self._inner = McpSessionFactory()

    async def connect(self, endpoint: DaemonEndpoint) -> DaemonSession:
        self.connects += 1
        return _LostResponseSession(await self._inner.connect(endpoint), self)


@dataclass(slots=True)
class _Harness:
    """Everything one acceptance scenario needs to address the live daemon."""

    root: Path
    foreign: Path
    endpoint: DaemonEndpoint
    adapters: dict[LanguageFamily, _Adapter]
    runtimes: list[WorkspaceRuntime] = field(default_factory=list)
    semantic_calls: list[str] = field(default_factory=list)

    @property
    def runtime(self) -> WorkspaceRuntime:
        assert len(self.runtimes) == 1, "expected exactly one shared workspace runtime"
        return self.runtimes[0]

    @property
    def python(self) -> _Adapter:
        return self.adapters[LanguageFamily.PYTHON]

    @property
    def typescript(self) -> _Adapter:
        return self.adapters[LanguageFamily.TYPESCRIPT]

    def connector(self, sessions: SessionFactory | None = None) -> Connector:
        return Connector(
            _StaticDiscovery(self.endpoint),
            sessions if sessions is not None else McpSessionFactory(),
            startup_cwd=self.root,
            heartbeat_interval_seconds=_QUIET_HEARTBEAT_SECONDS,
        )


def _policy(tmp_path: Path) -> tuple[WorkspacePolicy, Path]:
    data_root = tmp_path / "data"
    data_root.mkdir()
    prefix = tmp_path / "ms"
    purelib = prefix / "lib" / "python3.12" / "site-packages"
    transformers = purelib / "transformers"
    transformers.mkdir(parents=True)
    interpreter = prefix / "bin" / "python"
    interpreter.parent.mkdir()
    interpreter.write_text("#!/bin/sh\n")
    interpreter.chmod(0o755)
    return (
        WorkspacePolicy(
            conda_envs_root=tmp_path,
            data_root=data_root,
        ),
        data_root,
    )


def _repository(data_root: Path) -> Path:
    root = data_root / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    (root / ".gitignore").write_text("ignored.py\n")
    (root / "main.py").write_bytes(_SOURCE)
    (root / "spare.py").write_text("spare = 1\n")
    (root / "app.ts").write_text("export const value = 1;\n")
    (root / "ignored.py").write_text("secret = 1\n")
    return root


def _foreign_repository(data_root: Path) -> Path:
    """A sibling Git workspace used only to observe a typed boundary refusal."""

    root = data_root / "other"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    (root / "other.py").write_text("def target(): pass\n")
    return root


def _attributor(family: LanguageFamily) -> Callable[[Path, tuple[str, ...]], ScopeProjection]:
    """Stub only native attribution; every trust and edit decision stays real."""

    def attribute(root: Path, paths: tuple[str, ...]) -> ScopeProjection:
        del root
        return ScopeProjection.from_attribution(
            trust_inventory_paths=paths,
            attribution=NativeProgramAttribution(family, ProjectKind.WORKSPACE_DEFAULT, None, paths),
        )

    return attribute


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind((LOOPBACK_HOST, 0))
        return int(listener.getsockname()[1])


@contextmanager
def _serving(app: ASGIApp, port: int) -> Iterator[None]:
    """Serve the production daemon app on real loopback HTTP for one scenario."""

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=LOOPBACK_HOST,
            port=port,
            log_level="critical",
            access_log=False,
            timeout_graceful_shutdown=5,
        )
    )
    thread = threading.Thread(target=server.run, name="serena-light-acceptance-daemon", daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + _SERVER_TIMEOUT_SECONDS
        while not server.started:
            assert thread.is_alive(), "acceptance daemon exited before it served"
            assert time.monotonic() < deadline, "acceptance daemon did not become reachable"
            time.sleep(0.01)
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=_SERVER_TIMEOUT_SECONDS)
        assert not thread.is_alive(), "acceptance daemon did not stop"


@contextmanager
def _acceptance(
    tmp_path: Path,
    *,
    future_timeout: float = 35.0,
    transaction_queue_capacity: int | None = None,
) -> Iterator[_Harness]:
    """Compose the real daemon stack over a real Git workspace."""

    policy, data_root = _policy(tmp_path)
    root = _repository(data_root)
    foreign = _foreign_repository(data_root)
    adapters: dict[LanguageFamily, _Adapter] = {}
    runtimes: list[WorkspaceRuntime] = []
    semantic_calls: list[str] = []

    def build_adapter(context: AdapterBuildContext) -> _Adapter:
        adapter = _Adapter(context)
        adapters[context.family] = adapter
        return adapter

    def build_runtime(key: tuple[WorkspaceKind, Path, str, Path]) -> WorkspaceRuntime:
        kind, workspace_root, python_environment, python_interpreter = key
        runtime = WorkspaceRuntime(
            WorkspaceIdentity(
                root=workspace_root,
                kind=kind,
                working_subdirectory=workspace_root,
                python_environment=python_environment,
                python_interpreter=python_interpreter,
            ),
            path_policy=policy,
            attributors={family: _attributor(family) for family in _EXTENSIONS},
            adapter_factories={family: cast(AdapterFactory, build_adapter) for family in _EXTENSIONS},
            future_timeout=future_timeout,
            transaction_executor_factory=(
                None
                if transaction_queue_capacity is None
                else lambda _root: BoundedLspExecutor(
                    queue_capacity=transaction_queue_capacity,
                    name="acceptance-transaction",
                )
            ),
        )
        runtimes.append(runtime)
        return runtime

    service = _ObservedService(
        WorkspaceDaemonService[tuple[WorkspaceKind, Path, str, Path], WorkspaceRuntime](
            lifecycle=LeaseLifecycle(clock=time.monotonic),
            registry=WorkspaceRuntimeRegistry[
                tuple[WorkspaceKind, Path, str, Path],
                WorkspaceRuntime,
                UUID,
            ](build_runtime),
            resolver=policy,
            runtime_stopper=lambda runtime: runtime.stop(),
        ),
        semantic_calls,
    )
    daemon_id = str(uuid4())
    token = "c" * 48
    port = _free_loopback_port()
    app = create_daemon_app(
        service=cast(DaemonService, service),
        bearer=BearerSecret(token),
        daemon_id=daemon_id,
    )
    endpoint = DaemonEndpoint(
        daemon_id=daemon_id,
        url=f"http://{LOOPBACK_HOST}:{port}{MCP_PATH}",
        bearer=BearerSecret(token),
        protocol_version=types.LATEST_PROTOCOL_VERSION,
        server_version=__version__,
        build_identity=LEGACY_BUILD_IDENTITY,
    )
    with _serving(app, port):
        try:
            yield _Harness(
                root=root,
                foreign=foreign,
                endpoint=endpoint,
                adapters=adapters,
                runtimes=runtimes,
                semantic_calls=semantic_calls,
            )
        finally:
            for runtime in runtimes:
                with suppress(Exception):
                    runtime.stop()


@asynccontextmanager
async def _connected(harness: _Harness, sessions: SessionFactory | None = None) -> AsyncIterator[Connector]:
    connector = harness.connector(sessions)
    try:
        await connector.start()
        # A started connector holds only a lease: it binds its inherited startup
        # cwd lazily, on the first workspace-dependent call.  Force that binding
        # so every scenario below starts from an activated shared runtime.
        assert (await _call(connector, "get_runtime_status"))["ok"] is True
        assert connector.last_validated_binding == harness.root
        yield connector
    finally:
        await connector.aclose()


async def _call(connector: Connector, name: str, **arguments: object) -> Mapping[str, Any]:
    """Invoke one connector-visible tool and return its structured envelope."""

    result = await connector.call_tool(name, arguments)
    payload = result.structuredContent
    assert isinstance(payload, dict), f"{name} returned no structured envelope"
    return cast(Mapping[str, Any], payload)


async def _call_content(
    connector: Connector, name: str, **arguments: object
) -> tuple[types.CallToolResult, Mapping[str, Any]]:
    """Call the real connector and parse its public MCP text envelope.

    ``structuredContent`` is deliberately not used as the acceptance oracle:
    this helper makes the serialized ``CallToolResult.content`` payload the
    contract consumed by an MCP client.
    """

    result = await connector.call_tool(name, arguments)
    assert len(result.content) == 1
    content = result.content[0]
    assert isinstance(content, types.TextContent)
    payload = json.loads(content.text)
    assert isinstance(payload, dict)
    return result, cast(Mapping[str, Any], payload)


async def _replace(connector: Connector, expected_hash: str = _SOURCE_HASH) -> Mapping[str, Any]:
    return await _call(
        connector,
        "replace_symbol_body",
        name_path="target",
        relative_path="main.py",
        body=_NEW_BODY,
        expected_hash=expected_hash,
    )


async def _runtime_status(connector: Connector) -> Mapping[str, Any]:
    """Read shared-runtime status; ``status`` deliberately never scans itself."""

    payload = await _call(connector, "get_runtime_status")
    assert payload["ok"] is True, payload
    return cast(Mapping[str, Any], payload["data"]["runtime"])


async def _freshness(connector: Connector) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], (await _runtime_status(connector))["freshness"])


def _permit_edits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lift only the public reacceptance withholding, restored by the fixture."""

    monkeypatch.setattr(connector_module, "WITHHELD_TOOLS", frozenset())


def test_connector_activation_environment_warning_is_advisory_and_correctable(tmp_path: Path) -> None:
    environment = "llm-framework-study"
    interpreter = tmp_path / environment / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\n", encoding="utf-8")
    interpreter.chmod(0o755)
    target = tmp_path / environment / "lib" / "python3.12" / "site-packages"
    target.mkdir(parents=True)
    (target / "module.py").write_bytes(_SOURCE)

    with _acceptance(tmp_path) as harness:

        async def scenario() -> None:
            async with _connected(harness) as connector:
                default_result, default = await _call_content(
                    connector,
                    "activate_workspace",
                    absolute_path=str(target),
                )
                assert default_result.structuredContent == default
                default_data = cast(Mapping[str, object], default["data"])
                assert default_data["warnings"] == [
                    {
                        "code": "PYTHON_ENVIRONMENT_PATH_MISMATCH",
                        "selected_environment": "ms",
                        "path_environment": environment,
                        "next_action": "reactivate_with_path_environment",
                    }
                ]
                default_workspace = cast(Mapping[str, object], default_data["workspace"])
                assert default_workspace["python_environment"] == "ms"
                semantic = await _call(
                    connector,
                    "get_symbols_overview",
                    relative_path="module.py",
                )
                assert semantic["ok"] is True
                assert "warnings" not in semantic
                assert "warnings" not in cast(Mapping[str, object], semantic["data"])

                explicit = await _call(
                    connector,
                    "activate_workspace",
                    absolute_path=str(target),
                    python_environment=environment,
                )
                explicit_data = cast(Mapping[str, object], explicit["data"])
                assert "warnings" not in explicit_data
                explicit_workspace = cast(Mapping[str, object], explicit_data["workspace"])
                assert explicit_workspace["python_environment"] == environment

        asyncio.run(scenario())


def test_connector_scan_reports_create_change_delete_and_python_native_config(tmp_path: Path) -> None:
    with _acceptance(tmp_path) as harness:
        root = harness.root

        async def scenario() -> None:
            async with _connected(harness) as connector:
                assert (await _call(connector, "get_symbols_overview", relative_path="main.py"))["ok"] is True

                (root / "created.py").write_text("fresh = 1\n")
                (root / "main.py").write_bytes(_SOURCE + b"\n# appended\n")
                (root / "spare.py").unlink()
                (root / "pyrightconfig.json").write_text('{"include": ["."]}\n')

                assert (await _call(connector, "get_symbols_overview", relative_path="main.py"))["ok"] is True
                status = await _runtime_status(connector)

            # status() reports the latest completed guarded scan.  A
            # source-derived read also runs a clean postflight scan once the
            # preflight above has already reconciled every change, so the
            # reported scan is that later, empty pass.  The preflight
            # reconciliation itself is independently proven below by inventory
            # membership and by the watcher notification the affected python
            # adapter received.
            freshness = status["freshness"]
            assert freshness["created"] == []
            assert freshness["changed"] == []
            assert freshness["deleted"] == []
            assert freshness["config_changed"] == []
            assert freshness["opened"] == []
            assert freshness["reattributed"] == []
            assert freshness["notified"] == []
            assert "created.py" in harness.runtime.inventory.paths
            assert "spare.py" not in harness.runtime.inventory.paths
            assert "workspace/didChangeWatchedFiles" in harness.python.client.notifications
            # Only the family owning those paths and that native config moved,
            # and the untouched family stayed available rather than being retired.
            assert set(status["adapters"]) == {"python", "typescript"}
            assert status["unavailable_language_families"] == {}

        asyncio.run(scenario())


def test_connector_scan_reattributes_only_the_typescript_family(tmp_path: Path) -> None:
    with _acceptance(tmp_path) as harness:
        root = harness.root

        async def scenario() -> None:
            async with _connected(harness) as connector:
                assert (await _call(connector, "get_symbols_overview", relative_path="main.py"))["ok"] is True

                (root / "widget.ts").write_text("export const widget = 2;\n")
                (root / "tsconfig.json").write_text('{"include": ["."]}\n')

                assert (await _call(connector, "get_symbols_overview", relative_path="main.py"))["ok"] is True
                status = await _runtime_status(connector)

            # status() reports the latest completed guarded scan.  A
            # source-derived read also runs a clean postflight scan once the
            # preflight above has already reconciled the new file and config,
            # so the reported scan is that later, empty pass.  The preflight
            # reconciliation itself is independently proven below by inventory
            # membership and by the watcher notification the affected
            # typescript adapter received.
            freshness = status["freshness"]
            assert freshness["created"] == []
            assert freshness["config_changed"] == []
            assert freshness["changed"] == []
            assert freshness["deleted"] == []
            assert freshness["reattributed"] == []
            assert freshness["notified"] == []
            assert "widget.ts" in harness.runtime.inventory.paths
            assert "workspace/didChangeWatchedFiles" in harness.typescript.client.notifications
            assert set(status["adapters"]) == {"python", "typescript"}
            assert status["unavailable_language_families"] == {}

        asyncio.run(scenario())


def test_connector_edit_refuses_a_tracked_path_swapped_for_an_ignored_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _permit_edits(monkeypatch)
    with _acceptance(tmp_path) as harness:
        root = harness.root
        ignored = root / "ignored.py"
        tracked = root / "main.py"

        def substitute() -> None:
            tracked.unlink()
            tracked.symlink_to(ignored)

        async def scenario() -> None:
            async with _connected(harness) as connector:
                # The swap lands after the freshness scan and the first
                # authorization, so the under-lock component walk is the only
                # thing that can still refuse it.
                harness.python.before_edit = substitute
                result = await _replace(connector)

            assert result["error"]["code"] == "INVALID_PATH"
            assert ignored.read_text() == "secret = 1\n"
            assert tracked.is_symlink()
            assert harness.python.client.requests == []
            assert not list(root.glob(".*.serena-light-*.tmp"))

        asyncio.run(scenario())


def test_connector_edit_timing_out_while_queued_is_timed_out_and_never_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _permit_edits(monkeypatch)
    release = threading.Event()
    with _acceptance(tmp_path, future_timeout=0.05) as harness:
        try:

            async def scenario() -> None:
                async with _connected(harness) as connector:
                    occupied = harness.runtime.executor.submit(lambda: release.wait(5))
                    result = await _replace(connector)

                    assert result["error"]["code"] == "TIMED_OUT"
                    assert result["error"]["details"]["commit_state"] == "queued"
                    release.set()
                    assert occupied.result(timeout=5) is True
                    harness.runtime.executor.submit(lambda: None).result(timeout=5)
                    assert (harness.root / "main.py").read_bytes() == _SOURCE
                    assert harness.python.client.requests == []

            asyncio.run(scenario())
        finally:
            release.set()


def test_connector_edit_timing_out_while_running_is_uncertain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _permit_edits(monkeypatch)
    started = threading.Event()
    release = threading.Event()

    def block() -> None:
        started.set()
        assert release.wait(5)

    with _acceptance(tmp_path, future_timeout=0.05) as harness:
        try:

            async def scenario() -> None:
                async with _connected(harness) as connector:
                    harness.python.client.before_request = block
                    result = await _replace(connector)

                    assert started.is_set()
                    assert result["error"]["code"] == "UNCERTAIN"
                    assert result["error"]["details"]["commit_state"] == "running"
                    assert result["error"]["details"]["requires_current_reread"] is True

            asyncio.run(scenario())
        finally:
            release.set()


def test_connector_post_replace_directory_fsync_failure_is_uncertain_and_is_never_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _permit_edits(monkeypatch)
    real_fsync = os.fsync

    def fsync_failing_on_directories(file_descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
            raise OSError("directory flush failed")
        real_fsync(file_descriptor)

    with _acceptance(tmp_path) as harness:
        root = harness.root

        async def scenario() -> None:
            async with _connected(harness) as connector:
                monkeypatch.setattr(os, "fsync", fsync_failing_on_directories)
                result = await _replace(connector)
                installed = (root / "main.py").read_bytes()

                assert result["error"]["code"] == "UNCERTAIN"
                assert result["error"]["retry"] == {"retryable": False}
                assert result["error"]["details"]["uncertain_stage"] == "directory_fsync"
                assert result["error"]["details"]["current_hash"] == hashlib.sha256(installed).hexdigest()
                assert installed != _SOURCE
                assert not list(root.glob(".*.serena-light-*.tmp"))

                # The same expected hash must never be able to repeat the edit.
                monkeypatch.setattr(os, "fsync", real_fsync)
                assert (await _replace(connector))["error"]["code"] == "STALE_HASH"

        asyncio.run(scenario())


def test_connector_lost_edit_response_is_uncertain_and_is_never_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _permit_edits(monkeypatch)
    sessions = _LostResponseSessionFactory("replace_symbol_body")
    with _acceptance(tmp_path) as harness:

        async def scenario() -> None:
            async with _connected(harness, sessions) as connector:
                result = await _replace(connector)

                assert sessions.dropped is True
                assert result["error"]["code"] == "UNCERTAIN"
                assert result["error"]["retry"] == {"retryable": False}
                assert result["error"]["details"]["requires_current_reread"] is True
                assert result["error"]["details"]["operation"] == "replace_symbol_body"
                # The daemon executed the edit once; recovery must not replay it.
                assert harness.semantic_calls.count("replace_symbol_body") == 1
                assert (harness.root / "main.py").read_bytes() == _NEW_BODY.encode() + b"\n"

                # Recovery opened a replacement session and rebound the same
                # root, so its activation refresh observed the install.
                assert sessions.connects == 2
                assert connector.last_validated_binding == harness.root
                assert (await _freshness(connector))["changed"] == ["main.py"]

        asyncio.run(scenario())


def test_second_lease_on_the_same_root_reuses_the_runtime_and_refreshes_it(tmp_path: Path) -> None:
    with _acceptance(tmp_path) as harness:

        async def scenario() -> None:
            async with _connected(harness) as first:
                assert (await _freshness(first))["changed"] == []
                (harness.root / "main.py").write_bytes(_SOURCE + b"\n# appended\n")

                # A second lease activating the same root must refresh it before
                # any semantic operation runs on that connector.
                async with _connected(harness) as second:
                    assert (await _freshness(second))["changed"] == ["main.py"]

            assert len(harness.runtimes) == 1

        asyncio.run(scenario())


def test_connector_content_recovers_python_assignment_body_and_preserves_selection_range(
    tmp_path: Path,
) -> None:
    """The public MCP text envelope must never advertise an identifier as a body."""

    source = b"ANSWER: int = 42\r\n"
    identifier_range = {
        "start": {"line": 0, "character": 0},
        "end": {"line": 0, "character": 6},
    }
    symbol = {
        "name": "ANSWER",
        "kind": 14,
        "range": identifier_range,
        "selectionRange": identifier_range,
    }
    with _acceptance(tmp_path) as harness:
        (harness.root / "main.py").write_bytes(source)

        async def scenario() -> None:
            async with _connected(harness) as connector:
                harness.python.client.document_symbols = (symbol,)
                result, payload = await _call_content(
                    connector,
                    "find_symbol",
                    relative_path="main.py",
                    name_path="ANSWER",
                    include_body=True,
                    include_info=True,
                )

            assert result.isError is False
            assert payload["ok"] is True
            data = cast(Mapping[str, Any], payload["data"])
            file = cast(Mapping[str, Any], cast(list[Any], data["files"])[0])
            selected = cast(Mapping[str, Any], cast(list[Any], file["symbols"])[0])
            assert file["sha256"] == hashlib.sha256(source).hexdigest()
            assert selected["name_path"] == "ANSWER"
            assert selected["body"] == "ANSWER: int = 42"
            assert selected["range"] == [[0, 0], [0, 16]]
            assert "info" not in selected

        asyncio.run(scenario())


def test_connector_content_recovers_complete_typescript_variable_statement(tmp_path: Path) -> None:
    """The public MCP body includes declaration syntax, not only the binding suffix."""

    source = b"export const multiline = (\n  1 +\n  2\n);\n"
    selection = {
        "start": {"line": 0, "character": 13},
        "end": {"line": 0, "character": 22},
    }
    server_range = {
        "start": {"line": 0, "character": 13},
        "end": {"line": 3, "character": 1},
    }
    statement = {
        "start": {"line": 0, "character": 0},
        "end": {"line": 3, "character": 2},
    }
    symbol = {
        "name": "multiline",
        "kind": 14,
        "range": server_range,
        "selectionRange": selection,
    }
    with _acceptance(tmp_path) as harness:
        (harness.root / "main.ts").write_bytes(source)

        async def scenario() -> None:
            async with _connected(harness) as connector:
                harness.typescript.client.document_symbols = (symbol,)
                harness.typescript.client.selection_ranges = ({"range": selection, "parent": {"range": statement}},)
                result, payload = await _call_content(
                    connector,
                    "find_symbol",
                    relative_path="main.ts",
                    name_path="multiline",
                    include_body=True,
                    include_info=True,
                )

            assert result.isError is False
            assert payload["ok"] is True
            data = cast(Mapping[str, Any], payload["data"])
            file = cast(Mapping[str, Any], cast(list[Any], data["files"])[0])
            selected = cast(Mapping[str, Any], cast(list[Any], file["symbols"])[0])
            assert file["sha256"] == hashlib.sha256(source).hexdigest()
            assert selected["body"] == "export const multiline = (\n  1 +\n  2\n);"
            assert selected["range"] == [[0, 0], [3, 2]]
            assert "info" not in selected

        asyncio.run(scenario())


def test_connector_content_uses_zero_based_code_point_positions_after_astral_unicode(
    tmp_path: Path,
) -> None:
    """A UTF-16 LSP range becomes decoded-text coordinates in MCP content."""

    source = b'\xef\xbb\xbfprefix = "\xf0\x9f\x98\x80"; VALUE = 1\r\n'
    # ``VALUE`` begins at decoded-text column 14 but UTF-16 column 15 because
    # the preceding astral character consumes two UTF-16 code units.
    identifier_range = {
        "start": {"line": 0, "character": 15},
        "end": {"line": 0, "character": 20},
    }
    symbol = {
        "name": "VALUE",
        "kind": 14,
        "range": identifier_range,
        "selectionRange": identifier_range,
    }
    with _acceptance(tmp_path) as harness:
        (harness.root / "main.py").write_bytes(source)

        async def scenario() -> None:
            async with _connected(harness) as connector:
                harness.python.client.document_symbols = (symbol,)
                result, payload = await _call_content(
                    connector,
                    "find_symbol",
                    relative_path="main.py",
                    name_path="VALUE",
                    include_body=True,
                    include_info=True,
                )

            assert result.isError is False
            data = cast(Mapping[str, Any], payload["data"])
            file = cast(Mapping[str, Any], cast(list[Any], data["files"])[0])
            selected = cast(Mapping[str, Any], cast(list[Any], file["symbols"])[0])
            assert selected["body"] == "VALUE = 1"
            assert selected["range"] == [[0, 14], [0, 23]]
            assert "info" not in selected

        asyncio.run(scenario())


def test_connector_content_attaches_one_coverage_object_to_reference_successes(tmp_path: Path) -> None:
    """Both non-empty and empty semantic answers disclose one program scope."""

    with _acceptance(tmp_path) as harness:

        async def scenario() -> None:
            async with _connected(harness) as connector:
                harness.python.client.references = (
                    {
                        "uri": (harness.root / "main.py").as_uri(),
                        "range": {
                            "start": {"line": 0, "character": 4},
                            "end": {"line": 0, "character": 10},
                        },
                    },
                )
                result, non_empty_payload = await _call_content(
                    connector,
                    "find_referencing_symbols",
                    relative_path="main.py",
                    name_path="target",
                )
                harness.python.client.references = ()
                empty_result, empty_payload = await _call_content(
                    connector,
                    "find_referencing_symbols",
                    relative_path="main.py",
                    name_path="target",
                )

            assert result.isError is False
            assert empty_result.isError is False
            assert non_empty_payload["ok"] is True
            assert empty_payload["ok"] is True
            non_empty_data = cast(Mapping[str, Any], non_empty_payload["data"])
            empty_data = cast(Mapping[str, Any], empty_payload["data"])
            non_empty_files = cast(list[Mapping[str, Any]], non_empty_data["files"])
            assert len(non_empty_files) == 1
            assert len(cast(list[Any], non_empty_files[0]["references"])) == 1
            assert empty_data["files"] == []
            assert empty_data["omitted"] == 0
            coverage = cast(Mapping[str, Any], non_empty_data["coverage"])
            assert empty_data["coverage"] == coverage
            assert coverage == {"complete": True}
            assert all("coverage" not in reference for file in non_empty_files for reference in file["references"])

        asyncio.run(scenario())


def test_connector_parallel_same_workspace_reference_burst_has_no_sibling_not_ready(tmp_path: Path) -> None:
    """Nine public calls share one runtime without invalidating each other."""

    with _acceptance(tmp_path) as harness:

        async def scenario() -> list[tuple[types.CallToolResult, Mapping[str, Any]]]:
            async with _connected(harness) as connector:
                harness.python.client.references = (
                    {
                        "uri": (harness.root / "main.py").as_uri(),
                        "range": {
                            "start": {"line": 0, "character": 4},
                            "end": {"line": 0, "character": 10},
                        },
                    },
                )
                return await asyncio.gather(
                    *(
                        _call_content(
                            connector,
                            "find_referencing_symbols",
                            relative_path="main.py",
                            name_path="target",
                        )
                        for _ in range(9)
                    )
                )

        responses = asyncio.run(scenario())

        assert len(responses) == 9
        for result, payload in responses:
            assert result.isError is False
            assert payload["ok"] is True
            assert "error" not in payload
            data = cast(Mapping[str, Any], payload["data"])
            assert len(cast(list[Any], data["files"])) == 1
        assert harness.semantic_calls[-9:] == ["find_referencing_symbols"] * 9


def test_connector_transaction_queue_saturation_returns_busy_and_never_runs_rejected_call(tmp_path: Path) -> None:
    """The public daemon boundary preserves fixed transaction admission."""

    started = threading.Event()
    release = threading.Event()
    with _acceptance(tmp_path, transaction_queue_capacity=1) as harness:

        async def scenario() -> tuple[
            tuple[types.CallToolResult, Mapping[str, Any]],
            tuple[types.CallToolResult, Mapping[str, Any]],
            tuple[types.CallToolResult, Mapping[str, Any]],
        ]:
            async with _connected(harness) as connector:
                harness.python.client.references = ()

                def block_first_request() -> None:
                    started.set()
                    assert release.wait(5)

                harness.python.client.before_request = block_first_request
                first = asyncio.create_task(
                    _call_content(
                        connector,
                        "find_referencing_symbols",
                        relative_path="main.py",
                        name_path="target",
                    )
                )
                assert await asyncio.to_thread(started.wait, 5)
                second = asyncio.create_task(
                    _call_content(
                        connector,
                        "find_referencing_symbols",
                        relative_path="main.py",
                        name_path="target",
                    )
                )
                deadline = time.monotonic() + 5
                while harness.runtime.transaction_executor.snapshot().queue_size < 1:
                    assert time.monotonic() < deadline
                    await asyncio.sleep(0.001)

                rejected = await _call_content(
                    connector,
                    "find_referencing_symbols",
                    relative_path="main.py",
                    name_path="target",
                )
                release.set()
                return await first, await second, rejected

        first, second, rejected = asyncio.run(scenario())

        assert first[1]["ok"] is True
        assert second[1]["ok"] is True
        assert rejected[0].isError is False
        assert rejected[1]["ok"] is False
        rejected_error = cast(Mapping[str, Any], rejected[1]["error"])
        assert rejected_error["code"] == "BUSY"
        assert cast(Mapping[str, Any], rejected_error["retry"])["retryable"] is True
        # Only the two admitted operations ever reached the language server.
        assert harness.python.client.requests.count("textDocument/documentSymbol") == 2
        assert harness.python.client.requests.count("textDocument/references") == 4


def test_connector_content_preserves_external_references_as_raw_read_only_targets(tmp_path: Path) -> None:
    """The production connector keeps trusted external edges without inventing source text."""

    external = tmp_path / "ms" / "lib" / "python3.12" / "site-packages" / "external_pkg.py"
    with _acceptance(tmp_path) as harness:
        external.write_text("def target(): pass\n")

        async def scenario() -> None:
            async with _connected(harness) as connector:
                harness.python.client.references = (
                    {
                        "uri": external.as_uri(),
                        "range": {
                            "start": {"line": 0, "character": 4},
                            "end": {"line": 0, "character": 10},
                        },
                    },
                )
                result, payload = await _call_content(
                    connector,
                    "find_referencing_symbols",
                    relative_path="main.py",
                    name_path="target",
                )

            assert result.isError is False
            assert payload["ok"] is True
            data = cast(Mapping[str, Any], payload["data"])
            files = cast(list[Mapping[str, Any]], data["files"])
            assert len(files) == 1
            assert files[0]["path"] == str(external.resolve())
            assert files[0]["read_only"] is True
            assert set(files[0]) == {"path", "read_only", "references"}
            references = cast(list[Mapping[str, Any]], files[0]["references"])
            assert references == [
                {
                    "raw_range": [[0, 4], [0, 10]],
                    "position_basis": "lsp_zero_based_line_utf16_code_unit_character",
                    "symbol": "<file>",
                }
            ]
            assert data["coverage"] == {"complete": True}

        asyncio.run(scenario())


@dataclass(slots=True)
class _RacedExternalWrites:
    """Rewrite one trusted external target while an authoritative response runs.

    Every second reference response is the authoritative one of its transaction,
    and the guarded external digest that brackets it from below was already
    taken when this write lands.  ``pending`` holds one payload per attempt that
    must be raced.
    """

    writer: _ForeignWriter
    path: Path
    harness: _Harness
    pending: list[bytes]
    responses: int = 0

    def after_request(self, method: str, uri: str) -> None:
        del uri
        if method != "textDocument/references":
            return
        self.responses += 1
        if self.responses % 2 != 0 or not self.pending:
            return
        payload = self.pending.pop(0)
        self.writer.write(self.path, payload)
        # A server answers for the bytes it currently sees, so every later
        # response describes the rewritten file rather than the raced one.
        self.harness.python.client.references = _external_reference(self.path, line=payload.count(b"\n") - 1)


def _external_reference(external: Path, *, line: int = 0) -> tuple[Mapping[str, Any], ...]:
    return (
        {
            "uri": external.as_uri(),
            "range": {"start": {"line": line, "character": 4}, "end": {"line": line, "character": 10}},
        },
    )


def test_connector_raced_external_target_replays_to_the_settled_raw_location(tmp_path: Path) -> None:
    """An external raw range is only returned for bytes the read still witnesses.

    Another process rewrites the trusted external file while the authoritative
    reference response is produced.  Nothing inside the workspace moves, so only
    the external byte witness can report that race; the read replays once and
    returns the raw location its settled witness supports.
    """

    settled = b"# rewritten by another process\ndef target(): pass\n"
    with _acceptance(tmp_path) as harness, _foreign_writer() as writer:
        external = tmp_path / "ms" / "lib" / "python3.12" / "site-packages" / "external_pkg.py"
        external.write_bytes(b"def target(): pass\n")
        race = _RacedExternalWrites(writer, external, harness, [settled])

        async def scenario() -> tuple[types.CallToolResult, Mapping[str, Any]]:
            async with _connected(harness) as connector:
                harness.python.client.references = _external_reference(external)
                harness.python.client.after_request = race.after_request
                return await _call_content(
                    connector,
                    "find_referencing_symbols",
                    relative_path="main.py",
                    name_path="target",
                )

        result, payload = asyncio.run(scenario())
        writer.stop()

        assert writer.writes == [str(external)]
        assert race.pending == []
        assert external.read_bytes() == settled
        assert result.isError is False
        assert payload["ok"] is True
        data = cast(Mapping[str, Any], payload["data"])
        file = cast(Mapping[str, Any], cast(list[Any], data["files"])[0])
        assert file["path"] == str(external.resolve())
        assert file["read_only"] is True
        # The settled bytes moved the definition to line 1, and only that
        # settled raw range is returned.
        assert cast(list[Any], file["references"])[0]["raw_range"] == [[1, 4], [1, 10]]
        # Exactly two transactions: the raced one and the settled replay.
        assert harness.python.client.requests.count("textDocument/references") == 4


def test_connector_repeatedly_raced_external_target_returns_not_ready_without_a_raw_location(
    tmp_path: Path,
) -> None:
    """Continuous external rewriting fails retryably instead of returning a range."""

    second = b"# second\ndef target(): pass\n"
    third = b"# third\ndef target(): pass\n"
    with _acceptance(tmp_path) as harness, _foreign_writer() as writer:
        external = tmp_path / "ms" / "lib" / "python3.12" / "site-packages" / "external_pkg.py"
        external.write_bytes(b"def target(): pass\n")
        race = _RacedExternalWrites(writer, external, harness, [second, third])

        async def scenario() -> tuple[types.CallToolResult, Mapping[str, Any]]:
            async with _connected(harness) as connector:
                harness.python.client.references = _external_reference(external)
                harness.python.client.after_request = race.after_request
                return await _call_content(
                    connector,
                    "find_referencing_symbols",
                    relative_path="main.py",
                    name_path="target",
                )

        result, payload = asyncio.run(scenario())
        writer.stop()

        assert writer.writes == [str(external), str(external)]
        assert race.pending == []
        assert external.read_bytes() == third
        assert result.isError is False
        assert payload["ok"] is False
        assert "data" not in payload
        error_payload = cast(Mapping[str, Any], payload["error"])
        assert error_payload["code"] == "NOT_READY"
        assert error_payload["retry"]["retryable"] is True
        details = cast(Mapping[str, Any], error_payload["details"])
        assert details["reason"] == "workspace_changed_during_read"
        assert details["attempts"] == 2
        assert details["paths"] == [str(external.resolve())]
        # No attempt's raw external location may survive in the failure envelope.
        assert "raw_range" not in json.dumps(payload)
        assert harness.python.client.requests.count("textDocument/references") == 4


def test_stdio_proxy_withholds_editing_and_preserves_typed_boundary_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connector_module, "WITHHELD_TOOLS", frozenset({"replace_symbol_body"}))
    with _acceptance(tmp_path) as harness:
        foreign_operand = os.path.relpath(harness.foreign / "other.py", harness.root)

        async def scenario() -> None:
            client_send, server_receive = anyio.create_memory_object_stream[SessionMessage](0)
            server_send, client_receive = anyio.create_memory_object_stream[SessionMessage | Exception](0)
            async with _connected(harness) as connector:
                proxy: Server = build_proxy_server(connector)
                proxy_task = asyncio.create_task(
                    proxy.run(
                        server_receive,
                        server_send,
                        proxy.create_initialization_options(),
                        raise_exceptions=True,
                    ),
                    name="serena-light-acceptance-proxy",
                )
                try:
                    async with AsyncExitStack() as stack:
                        agent = await stack.enter_async_context(ClientSession(client_receive, client_send))
                        await agent.initialize()

                        listed = {tool.name for tool in (await agent.list_tools()).tools}
                        assert listed >= _WORKSPACE_TOOLS
                        assert "replace_symbol_body" not in listed
                        assert not listed & {"acquire_lease", "heartbeat", "release_lease", "get_daemon_status"}

                        withheld = await agent.call_tool(
                            "replace_symbol_body",
                            {
                                "name_path": "target",
                                "relative_path": "main.py",
                                "body": _NEW_BODY,
                                "expected_hash": _SOURCE_HASH,
                            },
                        )
                        refused = await agent.call_tool("get_symbols_overview", {"relative_path": foreign_operand})
                finally:
                    proxy_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await proxy_task

            assert withheld.isError is True
            withheld_payload = cast(Mapping[str, Any], withheld.structuredContent)
            assert withheld_payload["error"]["code"] == "UNSUPPORTED"
            assert withheld_payload["error"]["details"]["reason"] == "temporarily_disabled_pending_reacceptance"
            assert (harness.root / "main.py").read_bytes() == _SOURCE

            refused_payload = cast(Mapping[str, Any], refused.structuredContent)
            assert refused_payload["ok"] is False
            assert refused_payload["error"]["code"] == "OUT_OF_WORKSPACE"
            assert refused_payload["error"]["details"]["activation_hint"] == str(harness.foreign)
            assert refused_payload["workspace"]["root"] == str(harness.root)

        asyncio.run(scenario())


# --- Real daemon/connector freshness races driven by a separate writer process ---
#
# Every race below is ordered by explicit barriers: a foreign OS process
# rewrites the exact target only after the current attempt already owns that
# target's snapshot and symbols, and the attempt continues only once that
# process has acknowledged the completed write.  No case depends on timing.

_FOREIGN_WRITER_TIMEOUT_SECONDS = 20.0
_DIAGNOSTIC_MARKER = "# unresolved"


def _foreign_writer_main(connection: Connection) -> None:
    """Rewrite requested files in a separate process until asked to stop."""

    try:
        while True:
            command = connection.recv()
            if command is None:
                connection.send("stopped")
                return
            path, payload = cast(tuple[str, bytes], command)
            Path(path).write_bytes(payload)
            connection.send(hashlib.sha256(payload).hexdigest())
    finally:
        connection.close()


@dataclass(slots=True)
class _ForeignWriter:
    """A non-cooperating writer this test process can only reach across a pipe."""

    process: BaseProcess
    connection: Connection
    writes: list[str] = field(default_factory=list)

    def write(self, path: Path, payload: bytes) -> None:
        self.connection.send((str(path), payload))
        assert self.connection.poll(_FOREIGN_WRITER_TIMEOUT_SECONDS), "foreign writer did not acknowledge a write"
        assert self.connection.recv() == hashlib.sha256(payload).hexdigest()
        self.writes.append(str(path))

    def stop(self) -> None:
        self.connection.send(None)
        assert self.connection.poll(_FOREIGN_WRITER_TIMEOUT_SECONDS), "foreign writer did not acknowledge stop"
        assert self.connection.recv() == "stopped"
        self.process.join(_FOREIGN_WRITER_TIMEOUT_SECONDS)
        assert self.process.exitcode == 0, f"foreign writer exited with {self.process.exitcode}"


@contextmanager
def _foreign_writer() -> Iterator[_ForeignWriter]:
    """Start one writer process that shares no interpreter state with the daemon."""

    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(
        target=_foreign_writer_main,
        args=(child,),
        name="serena-light-acceptance-writer",
    )
    process.start()
    child.close()
    try:
        yield _ForeignWriter(process, parent)
    finally:
        if process.is_alive():
            process.kill()
            process.join(_FOREIGN_WRITER_TIMEOUT_SECONDS)
        assert not process.is_alive(), "foreign writer process was not cleaned up"
        parent.close()


@dataclass(slots=True)
class _RacedWrites:
    """Spend one queued foreign write per analyzed document, in order.

    ``pending`` holds one entry per attempt that must be raced.  The write is
    issued after that attempt's document symbols already answered, so the
    attempt owns a complete snapshot the postflight must reject.
    """

    writer: _ForeignWriter
    root: Path
    pending: list[tuple[str, bytes]]
    analyzed: list[str] = field(default_factory=list)

    def after_request(self, method: str, uri: str) -> None:
        if method != "textDocument/documentSymbol":
            return
        self.analyzed.append(uri)
        if not self.pending:
            return
        relative_path, payload = self.pending[0]
        if uri != (self.root / relative_path).resolve().as_uri():
            return
        self.pending.pop(0)
        self.writer.write(self.root / relative_path, payload)


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _analyzed_symbols(path: Path, *, definition: str, span_lines: int) -> tuple[Mapping[str, Any], ...]:
    """Answer for the current bytes of ``path`` in LSP UTF-16 coordinates.

    ``definition`` is the declaration prefix that introduces the symbol; the
    name, its position, and the reported range all come from the bytes on disk
    when the request arrives.  That is what makes an escaped first attempt
    visible: each attempt's answer belongs to the bytes that attempt analyzed.
    A range that starts after an astral character is only correct once
    production maps those UTF-16 code units onto decoded code points.
    """

    lines = path.read_bytes().decode("utf-8").split("\n")
    start_line = next(index for index, line in enumerate(lines) if definition in line)
    declaration = lines[start_line][lines[start_line].index(definition) :]
    name = declaration[len(definition) : declaration.index("(")]
    start_character = _utf16_length(lines[start_line][: lines[start_line].index(definition)])
    name_start = start_character + _utf16_length(definition)
    end_line = start_line + span_lines - 1
    return (
        {
            "name": name,
            "kind": 12,
            "range": {
                "start": {"line": start_line, "character": start_character},
                "end": {"line": end_line, "character": _utf16_length(lines[end_line])},
            },
            "selectionRange": {
                "start": {"line": start_line, "character": name_start},
                "end": {"line": start_line, "character": name_start + _utf16_length(name)},
            },
        },
    )


def _marker_diagnostics(text: str) -> tuple[Mapping[str, Any], ...]:
    """One finding per marker line of the exact text a generation analyzed."""

    return tuple(
        {
            "severity": 1,
            "message": "unresolved reference",
            "range": {
                "start": {"line": index, "character": 0},
                "end": {"line": index, "character": _utf16_length(line)},
            },
        }
        for index, line in enumerate(text.split("\n"))
        if _DIAGNOSTIC_MARKER in line
    )


@dataclass(frozen=True, slots=True)
class _RacedSymbol:
    """One file-scoped symbol whose bytes move while attempt one holds them."""

    id: str
    family: LanguageFamily
    relative_path: str
    definition: str
    span_lines: int
    initial: bytes
    settled: bytes
    body: str
    body_range: list[list[int]]


_RACED_SYMBOLS = (
    _RacedSymbol(
        id="python_shifted_body",
        family=LanguageFamily.PYTHON,
        relative_path="main.py",
        definition="def ",
        span_lines=2,
        initial=_SOURCE,
        settled=b"# rewritten by a foreign writer\ndef target():\n    return 2\n",
        body="def target():\n    return 2",
        body_range=[[1, 0], [2, 12]],
    ),
    _RacedSymbol(
        # The astral character before the symbol keeps UTF-16 and decoded
        # code-point columns distinguishable, and the foreign write adds one
        # more of them, so a first-attempt range cannot masquerade as settled.
        id="typescript_astral_columns",
        family=LanguageFamily.TYPESCRIPT,
        relative_path="main.ts",
        definition="export function ",
        span_lines=1,
        initial='const flag = "\U0001f600"; export function target() { return 1; }\n'.encode(),
        settled='const flag = "\U0001f600\U0001f600"; export function target() { return 2; }\n'.encode(),
        body="export function target() { return 2; }",
        body_range=[[0, 19], [0, 57]],
    ),
)


@pytest.mark.parametrize("case", _RACED_SYMBOLS, ids=lambda case: case.id)
def test_connector_raced_symbol_body_and_range_come_from_the_settled_replay(tmp_path: Path, case: _RacedSymbol) -> None:
    """A foreign process rewrites the exact target between snapshot and postflight."""

    with _acceptance(tmp_path) as harness, _foreign_writer() as writer:
        source = harness.root / case.relative_path
        source.write_bytes(case.initial)
        race = _RacedWrites(writer, harness.root, [(case.relative_path, case.settled)])

        async def scenario() -> tuple[types.CallToolResult, Mapping[str, Any]]:
            async with _connected(harness) as connector:
                adapter = harness.adapters[case.family]
                adapter.client.analyze = lambda uri: _analyzed_symbols(
                    source, definition=case.definition, span_lines=case.span_lines
                )
                adapter.client.after_request = race.after_request
                return await _call_content(
                    connector,
                    "find_symbol",
                    relative_path=case.relative_path,
                    name_path="target",
                    include_body=True,
                )

        result, payload = asyncio.run(scenario())
        writer.stop()

        assert writer.writes == [str(source)]
        assert race.pending == []
        assert source.read_bytes() == case.settled
        assert result.isError is False
        assert payload["ok"] is True
        data = cast(Mapping[str, Any], payload["data"])
        file = cast(Mapping[str, Any], cast(list[Any], data["files"])[0])
        selected = cast(Mapping[str, Any], cast(list[Any], file["symbols"])[0])
        assert selected["body"] == case.body
        assert selected["range"] == case.body_range
        assert file["sha256"] == hashlib.sha256(case.settled).hexdigest()
        # Exactly two attempts: the raced one and the settled replay.
        assert harness.adapters[case.family].client.requests.count("textDocument/documentSymbol") == 2
        assert len(race.analyzed) == 2


def test_connector_two_raced_attempts_return_retryable_not_ready_without_source_payload(tmp_path: Path) -> None:
    """Continuous foreign writing fails retryably instead of returning any body."""

    second = b"def target():\n    return 2\n"
    third = b"def target():\n    return 3\n"
    with _acceptance(tmp_path) as harness, _foreign_writer() as writer:
        source = harness.root / "main.py"
        race = _RacedWrites(writer, harness.root, [("main.py", second), ("main.py", third)])

        async def scenario() -> tuple[types.CallToolResult, Mapping[str, Any]]:
            async with _connected(harness) as connector:
                harness.python.client.after_request = race.after_request
                return await _call_content(
                    connector,
                    "find_symbol",
                    relative_path="main.py",
                    name_path="target",
                    include_body=True,
                )

        result, payload = asyncio.run(scenario())
        writer.stop()

        assert writer.writes == [str(source), str(source)]
        assert race.pending == []
        assert source.read_bytes() == third
        # A retryable freshness failure is a typed envelope, not a protocol error.
        assert result.isError is False
        assert payload["ok"] is False
        error_payload = cast(Mapping[str, Any], payload["error"])
        assert error_payload["code"] == "NOT_READY"
        assert error_payload["retry"]["retryable"] is True
        details = cast(Mapping[str, Any], error_payload["details"])
        assert details["reason"] == "workspace_changed_during_read"
        assert details["attempts"] == 2
        assert details["paths"] == ["main.py"]
        assert "data" not in payload
        # No attempt's source authority may survive in the failure envelope.
        serialized = json.dumps(payload)
        assert all(body not in serialized for body in ("return 1", "return 2", "return 3"))
        assert harness.python.client.requests.count("textDocument/documentSymbol") == 2


def test_connector_raced_reference_target_returns_only_settled_reference_authority(tmp_path: Path) -> None:
    """A response-owned reference target rewritten during attempt one is replayed."""

    # The referencing line keeps its position so both attempts can map the same
    # reference; only the container name and the line's text move.
    initial = b"from main import target\n\n\ndef caller():\n    return target()\n"
    settled = b"from main import target\n\n\ndef caller_renamed():\n    return target()  # rewritten\n"
    with _acceptance(tmp_path) as harness, _foreign_writer() as writer:
        spare = harness.root / "spare.py"
        spare.write_bytes(initial)
        race = _RacedWrites(writer, harness.root, [("spare.py", settled)])

        async def scenario() -> tuple[types.CallToolResult, Mapping[str, Any]]:
            async with _connected(harness) as connector:
                main_uri = (harness.root / "main.py").resolve().as_uri()
                harness.python.client.analyze = lambda uri: (
                    _SYMBOLS if uri == main_uri else _analyzed_symbols(spare, definition="def ", span_lines=2)
                )
                harness.python.client.references = (
                    {
                        "uri": spare.resolve().as_uri(),
                        "range": {
                            "start": {"line": 4, "character": 11},
                            "end": {"line": 4, "character": 17},
                        },
                    },
                )
                harness.python.client.after_request = race.after_request
                return await _call_content(
                    connector,
                    "find_referencing_symbols",
                    relative_path="main.py",
                    name_path="target",
                    max_snippet_chars=120,
                )

        result, payload = asyncio.run(scenario())
        writer.stop()

        assert writer.writes == [str(spare)]
        assert race.pending == []
        assert spare.read_bytes() == settled
        assert result.isError is False
        assert payload["ok"] is True
        data = cast(Mapping[str, Any], payload["data"])
        file = cast(Mapping[str, Any], cast(list[Any], data["files"])[0])
        assert file["path"] == "spare.py"
        reference = cast(Mapping[str, Any], cast(list[Any], file["references"])[0])
        assert reference["range"] == [[4, 11], [4, 17]]
        assert reference["symbol"] == "caller_renamed"
        assert reference["snippet"] == "    return target()  # rewritten"
        # Exactly two attempts, each opening the source and its one target.
        assert harness.python.client.requests.count("textDocument/documentSymbol") == 4
        assert harness.python.client.requests.count("textDocument/references") == 4


def test_connector_raced_clean_diagnostics_replay_returns_the_settled_findings(tmp_path: Path) -> None:
    """A clean first attempt cannot escape once its file gains a finding."""

    settled = _SOURCE + f"call_missing()  {_DIAGNOSTIC_MARKER}\n".encode()
    with _acceptance(tmp_path) as harness, _foreign_writer() as writer:
        source = harness.root / "main.py"
        race = _RacedWrites(writer, harness.root, [("main.py", settled)])

        async def scenario() -> tuple[types.CallToolResult, Mapping[str, Any]]:
            async with _connected(harness) as connector:
                harness.python.diagnose = _marker_diagnostics
                harness.python.client.after_request = race.after_request
                return await _call_content(connector, "get_diagnostics_for_file", relative_path="main.py")

        result, payload = asyncio.run(scenario())
        writer.stop()

        assert writer.writes == [str(source)]
        assert race.pending == []
        assert source.read_bytes() == settled
        assert result.isError is False
        assert payload["ok"] is True
        data = cast(Mapping[str, Any], payload["data"])
        assert data["workspace"] == str(harness.root)
        files = cast(list[Mapping[str, Any]], data["files"])
        assert len(files) == 1 and files[0]["path"] == "main.py"
        findings = cast(list[Mapping[str, Any]], files[0]["diagnostics"])
        assert len(findings) == 1
        assert findings[0]["message"] == "unresolved reference"
        assert findings[0]["severity"] == "error"
        assert cast(list[list[int]], findings[0]["range"])[0][0] == _SOURCE.count(b"\n")
        assert data["omitted"] == 0
        assert harness.python.client.requests.count("textDocument/documentSymbol") == 2
