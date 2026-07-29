"""Private daemon process used only by daemon fault acceptance tests.

The process deliberately composes the shipped HTTP server, connector-facing
service, lease lifecycle, bounded executor, and parent-death launcher.  Its
runtime methods are small deterministic stand-ins for an LSP request so the
tests can place SIGKILL between request acceptance and result delivery without
teaching production code a test-only fault hook.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

import psutil
import uvicorn

from serena_light.daemon.leases import LeaseLifecycle
from serena_light.daemon.server import LOOPBACK_HOST, create_daemon_app
from serena_light.daemon.service import WorkspaceDaemonService
from serena_light.lsp.executor import BoundedLspExecutor
from serena_light.processes import LanguageServerSubprocessLauncher, terminate_process_tree_with_kill_fallback
from serena_light.runtime_files import BearerSecret
from serena_light.workspace.registry import ResolvedWorkspace, WorkspaceRuntimeRegistry


def _record(path: Path, event: str, **data: object) -> None:
    payload = {"event": event, "at": time.monotonic(), **data}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class AcceptanceRuntime:
    def __init__(self, identity: str, *, state_path: Path, block_crash_operations: bool) -> None:
        self.identity = identity
        self._state_path = state_path
        self._block_crash_operations = block_crash_operations
        self._executor = BoundedLspExecutor(queue_capacity=2, name=Path(identity).name)
        self._child = LanguageServerSubprocessLauncher.get_instance().launch(
            [sys.executable, "-c", "import time; time.sleep(600)"], cwd="/"
        )
        child = psutil.Process(self._child.pid)
        _record(
            self._state_path,
            "language_server_started",
            identity=identity,
            pid=self._child.pid,
            create_time=child.create_time(),
            process_group=os.getpgid(self._child.pid),
        )

    def stop(self) -> None:
        self._executor.close()
        if self._child.poll() is None:
            terminate_process_tree_with_kill_fallback(self._child, 1.0, "acceptance language server")

    def status(self) -> Mapping[str, object]:
        snapshot = self._executor.snapshot()
        return {
            "identity": self.identity,
            "executor": {
                "active": snapshot.active,
                "queue_size": snapshot.queue_size,
                "queue_capacity": snapshot.queue_capacity,
                "actual_worker_count": sum(
                    thread.name == f"serena-light-lsp:{Path(self.identity).name}" for thread in threading.enumerate()
                ),
            },
        }

    def find_symbol(self, *, name_path: str, **_kwargs: object) -> Mapping[str, object]:
        return self._executor.submit(lambda: self._read(name_path)).result()

    def replace_symbol_body(self, **_kwargs: object) -> Mapping[str, object]:
        return self._executor.submit(lambda: self._edit()).result()

    def _read(self, name_path: str) -> Mapping[str, object]:
        if name_path == "long":
            _record(self._state_path, "long_read_started", identity=self.identity)
            time.sleep(62.0)
        elif name_path == "crash-read" and self._block_crash_operations:
            _record(self._state_path, "crash_read_started", identity=self.identity)
            time.sleep(300.0)
        return {"ok": True, "data": {"name_path": name_path, "identity": self.identity}}

    def _edit(self) -> Mapping[str, object]:
        if self._block_crash_operations:
            _record(self._state_path, "edit_started", identity=self.identity)
            time.sleep(300.0)
        return {"ok": True, "data": {"edited": True, "identity": self.identity}}


class ObservedService:
    def __init__(self, service: WorkspaceDaemonService[str, AcceptanceRuntime], state_path: Path) -> None:
        self._service = service
        self._state_path = state_path

    async def status(self, *, mcp_session_id: str) -> Mapping[str, object]:
        return await self._service.status(mcp_session_id=mcp_session_id)

    async def acquire_lease(self, *, mcp_session_id: str) -> Mapping[str, object]:
        return await self._service.acquire_lease(mcp_session_id=mcp_session_id)

    async def heartbeat(self, *, lease_id: str) -> Mapping[str, object]:
        result = await self._service.heartbeat(lease_id=lease_id)
        _record(self._state_path, "heartbeat", lease_id=lease_id)
        return result

    async def release_lease(self, *, lease_id: str, immediate: bool) -> Mapping[str, object]:
        return await self._service.release_lease(lease_id=lease_id, immediate=immediate)

    async def activate_workspace(self, *, lease_id: str, absolute_path: str) -> Mapping[str, object]:
        return await self._service.activate_workspace(lease_id=lease_id, absolute_path=absolute_path)

    async def release_workspace(
        self, *, lease_id: str, immediate: bool = False
    ) -> Mapping[str, object]:
        return await self._service.release_workspace(lease_id=lease_id, immediate=immediate)

    async def get_runtime_status(self, *, lease_id: str) -> Mapping[str, object]:
        return await self._service.get_runtime_status(lease_id=lease_id)

    async def semantic_operation(self, *, lease_id: str, operation: str, **kwargs: object) -> Mapping[str, object]:
        return await self._service.semantic_operation(lease_id=lease_id, operation=operation, **kwargs)


async def _run(arguments: argparse.Namespace) -> None:
    state_path = Path(arguments.state).resolve()
    root = Path(arguments.root).resolve()
    root.mkdir(parents=True, exist_ok=True)

    def resolver(path: Path) -> ResolvedWorkspace[str]:
        resolved = path.resolve()
        return ResolvedWorkspace(identity=str(resolved), working_subdirectory=resolved)

    def runtime_factory(identity: str) -> AcceptanceRuntime:
        return AcceptanceRuntime(
            identity,
            state_path=state_path,
            block_crash_operations=arguments.block_crash_operations,
        )

    registry = WorkspaceRuntimeRegistry(runtime_factory)
    concrete = WorkspaceDaemonService[str, AcceptanceRuntime](
        lifecycle=LeaseLifecycle(clock=time.monotonic),
        registry=registry,
        resolver=resolver,
        runtime_stopper=lambda runtime: runtime.stop(),
    )
    service = ObservedService(concrete, state_path)
    app = create_daemon_app(service=service, bearer=BearerSecret(arguments.token), daemon_id=arguments.daemon_id)
    server = uvicorn.Server(
        uvicorn.Config(app, host=LOOPBACK_HOST, port=arguments.port, access_log=False, log_level="critical")
    )
    task = asyncio.create_task(server.serve(), name="acceptance-daemon-http")
    try:
        deadline = time.monotonic() + 10.0
        while not server.started:
            if task.done():
                await task
            if time.monotonic() >= deadline:
                raise TimeoutError("acceptance daemon did not start")
            await asyncio.sleep(0.01)
        _record(
            state_path,
            "ready",
            daemon_id=arguments.daemon_id,
            pid=os.getpid(),
            create_time=psutil.Process().create_time(),
            port=arguments.port,
        )
        await task
    finally:
        server.should_exit = True
        for state in tuple(registry._runtimes.values()):  # private only in this test-only process owner
            state.runtime.stop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--token", required=True)
    parser.add_argument("--daemon-id", default=str(uuid4()))
    parser.add_argument("--block-crash-operations", action="store_true")
    arguments = parser.parse_args()
    asyncio.run(_run(arguments))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
