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
"""

from __future__ import annotations

import asyncio
import hashlib
import json
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
from serena_light.lsp.positions import FileSnapshot, PositionEncoding
from serena_light.runtime_files import LEGACY_BUILD_IDENTITY, BearerSecret
from serena_light.tools.editing import NotificationResult, ReplacementNotification
from serena_light.workspace.identity import (
    PinnedMsRoots,
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


class _Client:
    """Deterministic stand-in for one language-server client connection."""

    def __init__(self) -> None:
        self.before_request: Callable[[], None] | None = None
        self.requests: list[str] = []
        self.notifications: list[str] = []
        self.document_symbols: tuple[Mapping[str, Any], ...] = _SYMBOLS
        self.selection_ranges: tuple[Mapping[str, Any], ...] = ()
        self.references: tuple[Mapping[str, Any], ...] = ()

    def request(self, method: str, params: object = None, *, timeout: float | None = None) -> object:
        del params, timeout
        self.requests.append(method)
        if self.before_request is not None:
            self.before_request()
        if method == "textDocument/documentSymbol":
            return list(self.document_symbols)
        if method == "textDocument/selectionRange":
            return list(self.selection_ranges)
        if method == "textDocument/references":
            return list(self.references)
        return list(_SYMBOLS)

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
            EngineMetadata("fake", "1.0", Path("/owned/server"), Path("/owned/python")),
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
            self.document_generation += 1
            target = DocumentReadinessTarget(uri, relative_path, absolute_path, version, self.document_generation, 0)
            assert probe.observe(self.client, target, timeout=1.0)
            return FileSnapshot.from_bytes(absolute_path.read_bytes()), target

        return self.context.executor.submit(worker)

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
        service: WorkspaceDaemonService[tuple[WorkspaceKind, Path], WorkspaceRuntime],
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

    async def activate_workspace(self, *, lease_id: str, absolute_path: str) -> Mapping[str, object]:
        return await self._service.activate_workspace(lease_id=lease_id, absolute_path=absolute_path)

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

    async def activate_workspace(self, lease_id: str, path: Path) -> types.CallToolResult:
        return await self._inner.activate_workspace(lease_id, path)

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
    interpreter.touch()
    return (
        WorkspacePolicy(
            ms_roots=PinnedMsRoots(
                interpreter=interpreter.resolve(),
                stdlib=purelib.parent.resolve(),
                purelib=purelib.resolve(),
                platlib=purelib.resolve(),
                conda_prefix=prefix.resolve(),
            ),
            allowed_non_git_root=transformers,
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
def _acceptance(tmp_path: Path, *, future_timeout: float = 35.0) -> Iterator[_Harness]:
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

    def build_runtime(key: tuple[WorkspaceKind, Path]) -> WorkspaceRuntime:
        kind, workspace_root = key
        runtime = WorkspaceRuntime(
            WorkspaceIdentity(root=workspace_root, kind=kind, working_subdirectory=workspace_root),
            path_policy=policy,
            attributors={family: _attributor(family) for family in _EXTENSIONS},
            adapter_factories={family: cast(AdapterFactory, build_adapter) for family in _EXTENSIONS},
            future_timeout=future_timeout,
        )
        runtimes.append(runtime)
        return runtime

    service = _ObservedService(
        WorkspaceDaemonService[tuple[WorkspaceKind, Path], WorkspaceRuntime](
            lifecycle=LeaseLifecycle(clock=time.monotonic),
            registry=WorkspaceRuntimeRegistry[tuple[WorkspaceKind, Path], WorkspaceRuntime, UUID](build_runtime),
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
            assert set(coverage) == {
                "adapter",
                "language",
                "scope_kind",
                "configured_program_files",
                "configured_program_digest",
                "trusted_language_files",
                "trusted_language_digest",
                "uncovered_files",
                "uncovered_sample",
            }
            assert coverage["adapter"] == "pyright"
            assert coverage["language"] == "python"
            assert isinstance(coverage["configured_program_files"], int)
            assert isinstance(coverage["trusted_language_files"], int)
            sample = cast(Mapping[str, Any], coverage["uncovered_sample"])
            assert set(sample) == {"total", "items", "digest", "omitted"}
            assert sample["total"] == coverage["uncovered_files"]
            assert isinstance(sample["items"], list)
            assert sample["omitted"] == sample["total"] - len(sample["items"])
            assert all("coverage" not in reference for file in non_empty_files for reference in file["references"])

        asyncio.run(scenario())


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
            coverage = cast(Mapping[str, Any], data["coverage"])
            assert coverage["adapter"] == "pyright"
            assert coverage["language"] == "python"
            assert coverage["scope_kind"] == "workspace_default"

        asyncio.run(scenario())


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
