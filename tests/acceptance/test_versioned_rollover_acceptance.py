"""Acceptance for coexistence and retirement across versioned daemon slots."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

from mcp import types
from mcp.types import LATEST_PROTOCOL_VERSION

from serena_light import __version__, cli
from serena_light.connector import (
    Connector,
    DaemonEndpoint,
    LeaseGrant,
    RuntimeDiscoveryProvider,
)
from serena_light.daemon.leases import LeaseEndReason, LeaseLifecycle
from serena_light.daemon.service import WorkspaceDaemonService
from serena_light.runtime_files import (
    BEARER_NAME,
    DISCOVERY_NAME,
    DiscoveryMetadata,
    create_bearer_secret,
    prepare_runtime_layout,
    read_bearer_secret,
    write_discovery_metadata,
)
from serena_light.workspace.registry import ResolvedWorkspace, WorkspaceRuntimeRegistry


@dataclass
class _Clock:
    now: float = 100.0

    def __call__(self) -> float:
        return self.now


@dataclass
class _Runtime:
    root: Path
    build_identity: str
    stopped: bool = False

    def status(self) -> Mapping[str, object]:
        return {
            "root": str(self.root),
            "build_identity": self.build_identity,
            "stopped": self.stopped,
        }


@dataclass
class _Build:
    identity: str
    daemon_id: str
    build_root: Path
    endpoint: DaemonEndpoint
    clock: _Clock
    lifecycle: LeaseLifecycle[Path, _Runtime]
    service: WorkspaceDaemonService[Path, _Runtime]
    runtimes: dict[Path, _Runtime]
    stopped: list[_Runtime]


def _tool_result(payload: Mapping[str, object]) -> types.CallToolResult:
    rendered = dict(payload)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(rendered, sort_keys=True))],
        structuredContent=rendered,
        isError=rendered.get("ok") is False,
    )


class _DirectDaemonSession:
    """Exercise Connector lifecycle against the production daemon service seam."""

    def __init__(self, build: _Build) -> None:
        self._build = build

    async def acquire_lease(self) -> LeaseGrant:
        data = await self._build.service.acquire_lease(mcp_session_id=str(uuid4()))
        return LeaseGrant(cast(str, data["lease_id"]), self._build.daemon_id)

    async def heartbeat(self, lease_id: str) -> None:
        await self._build.service.heartbeat(lease_id=lease_id)

    async def release_lease(self, lease_id: str) -> None:
        await self._build.service.release_lease(lease_id=lease_id, immediate=False)

    async def activate_workspace(
        self,
        lease_id: str,
        path: Path,
        python_environment: str | None = None,
    ) -> types.CallToolResult:
        data = await self._build.service.activate_workspace(
            lease_id=lease_id,
            absolute_path=str(path),
            python_environment=python_environment,
        )
        return _tool_result({"ok": True, "data": dict(data)})

    async def list_tools(self) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[])

    async def call_tool(
        self,
        lease_id: str,
        name: str,
        arguments: Mapping[str, object] | None,
    ) -> types.CallToolResult:
        assert arguments is None
        assert name == "get_runtime_status"
        return _tool_result(await self._build.service.get_runtime_status(lease_id=lease_id))

    async def aclose(self) -> None:
        return None


class _DirectSessionFactory:
    def __init__(self, build: _Build) -> None:
        self._build = build

    async def connect(self, endpoint: DaemonEndpoint) -> _DirectDaemonSession:
        assert endpoint == self._build.endpoint
        return _DirectDaemonSession(self._build)


def _create_build(runtime_root: Path, identity: str, port: int) -> _Build:
    layout = prepare_runtime_layout(runtime_root, identity)
    bearer = create_bearer_secret(layout.build_root)
    daemon_id = str(uuid4())
    metadata = DiscoveryMetadata.create(
        daemon_id=daemon_id,
        pid=os.getpid(),
        process_start_time=1.0,
        endpoint=f"http://127.0.0.1:{port}/mcp",
        protocol_version=LATEST_PROTOCOL_VERSION,
        server_version=__version__,
        build_identity=identity,
    )
    write_discovery_metadata(layout.build_root, metadata)

    clock = _Clock()
    runtimes: dict[Path, _Runtime] = {}
    stopped: list[_Runtime] = []

    def create_runtime(root: Path) -> _Runtime:
        runtime = _Runtime(root, identity)
        runtimes[root] = runtime
        return runtime

    def stop_runtime(runtime: _Runtime) -> None:
        runtime.stopped = True
        stopped.append(runtime)

    def resolve(path: Path, _python_environment: str) -> ResolvedWorkspace[Path]:
        root = path.resolve()
        return ResolvedWorkspace(identity=root, working_subdirectory=root)

    lifecycle = LeaseLifecycle[Path, _Runtime](
        clock=clock,
        heartbeat_interval_seconds=1.0,
        expiry_seconds=1_000.0,
        warm_grace_seconds=10.0,
    )
    service = WorkspaceDaemonService(
        lifecycle=lifecycle,
        registry=WorkspaceRuntimeRegistry(create_runtime),
        resolver=resolve,
        runtime_stopper=stop_runtime,
    )
    return _Build(
        identity=identity,
        daemon_id=daemon_id,
        build_root=layout.build_root,
        endpoint=DaemonEndpoint.from_runtime_files(metadata, bearer),
        clock=clock,
        lifecycle=lifecycle,
        service=service,
        runtimes=runtimes,
        stopped=stopped,
    )


def _connector(build: _Build, root: Path) -> Connector:
    discovery = RuntimeDiscoveryProvider(
        runtime_root=build.build_root,
        is_process_identity_live=lambda _pid, _created: True,
    )
    return Connector(
        discovery,
        _DirectSessionFactory(build),
        startup_cwd=root,
        heartbeat_interval_seconds=3_600.0,
    )


def _status_payload(result: types.CallToolResult) -> Mapping[str, object]:
    assert result.structuredContent is not None
    payload = cast(Mapping[str, object], result.structuredContent)
    assert payload["ok"] is True
    return cast(Mapping[str, object], payload["data"])


def test_versioned_rollover_preserves_live_old_build_and_retires_only_after_grace(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runtime_root = tmp_path / "runtime"
        workspace_a = tmp_path / "workspace-a"
        workspace_b = tmp_path / "workspace-b"
        workspace_a.mkdir()
        workspace_b.mkdir()

        old = _create_build(runtime_root, "a" * 64, 43101)
        new = _create_build(runtime_root, "b" * 64, 43102)
        old_a = _connector(old, workspace_a)
        old_b = _connector(old, workspace_b)
        new_b = _connector(new, workspace_b)
        connectors = (old_a, old_b, new_b)

        try:
            await old_a.start()
            await old_b.start()
            await old_a.call_tool("get_runtime_status")
            await old_b.call_tool("get_runtime_status")
            old_lease_ids = {old_a.lease_id, old_b.lease_id}
            assert None not in old_lease_ids
            assert len(old_lease_ids) == 2
            assert old.lifecycle.active_lease_count() == 2
            assert set(old.runtimes) == {workspace_a.resolve(), workspace_b.resolve()}

            await new_b.start()
            await new_b.call_tool("get_runtime_status")
            assert old.build_root != new.build_root
            assert old.build_root.joinpath(DISCOVERY_NAME).exists()
            assert new.build_root.joinpath(DISCOVERY_NAME).exists()
            assert new.lifecycle.active_lease_count() == 1
            assert set(new.runtimes) == {workspace_b.resolve()}

            status_a = _status_payload(await old_a.call_tool("get_runtime_status"))
            status_b = _status_payload(await old_b.call_tool("get_runtime_status"))
            assert cast(Mapping[str, object], status_a["binding"])["identity"] == str(workspace_a.resolve())
            assert cast(Mapping[str, object], status_b["binding"])["identity"] == str(workspace_b.resolve())
            assert old.lifecycle.active_lease_count() == 2
            assert not any(runtime.stopped for runtime in old.runtimes.values())

            await old_a.aclose()
            assert old.lifecycle.active_lease_count() == 1
            assert not old.service.daemon_idle()
            await old_b.aclose()
            assert old.lifecycle.active_lease_count() == 0
            assert not old.service.daemon_idle()

            old.clock.now = 109.999
            assert await old.service.sweep() == ()
            assert not old.service.daemon_idle()
            assert old.stopped == []

            old.clock.now = 110.0
            decisions = await old.service.sweep()
            assert len(decisions) == 2
            assert {decision.reason for decision in decisions} == {LeaseEndReason.GRACE_EXPIRED}
            assert all(decision.active_holders == 0 for decision in decisions)
            assert old.service.daemon_idle()
            assert set(map(id, old.stopped)) == set(map(id, old.runtimes.values()))

            assert new.lifecycle.active_lease_count() == 1
            assert not new.service.daemon_idle()
            new_status = _status_payload(await new_b.call_tool("get_runtime_status"))
            assert cast(Mapping[str, object], new_status["binding"])["identity"] == str(workspace_b.resolve())

            successor_id = str(uuid4())
            successor_bearer = create_bearer_secret(old.build_root)
            successor = DiscoveryMetadata.create(
                daemon_id=successor_id,
                pid=os.getpid(),
                process_start_time=2.0,
                endpoint="http://127.0.0.1:43103/mcp",
                protocol_version=LATEST_PROTOCOL_VERSION,
                server_version=__version__,
                build_identity=old.identity,
            )
            write_discovery_metadata(old.build_root, successor)
            new_discovery_before = new.build_root.joinpath(DISCOVERY_NAME).read_bytes()

            cli._remove_owned_runtime_artifacts(old.build_root, old.daemon_id)

            assert json.loads(old.build_root.joinpath(DISCOVERY_NAME).read_text())["daemon_id"] == successor_id
            assert read_bearer_secret(old.build_root) == successor_bearer
            assert old.build_root.joinpath(BEARER_NAME).exists()
            assert new.build_root.joinpath(DISCOVERY_NAME).read_bytes() == new_discovery_before
        finally:
            for connector in reversed(connectors):
                await connector.aclose()

    asyncio.run(scenario())
