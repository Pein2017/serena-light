"""Real executable/process acceptance for isolated versioned daemon rollover."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx
import psutil
import pytest
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client

from serena_light import cli
from serena_light.bootstrap import runtime_paths
from serena_light.runtime_files import (
    BEARER_NAME,
    DISCOVERY_NAME,
    RUNTIME_ROOT,
    DiscoveryMetadata,
    RuntimeFileError,
    prepare_runtime_layout,
    read_bearer_secret,
    read_discovery_metadata,
)

pytestmark = pytest.mark.timeout(60)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_OLD_VARIANT = "old"
_NEW_VARIANT = "new"
_WARM_GRACE_SECONDS = 1.5
_IDLE_EXIT_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class _ProcessIdentity:
    pid: int
    create_time: float


def _is_live(identity: _ProcessIdentity) -> bool:
    try:
        process = psutil.Process(identity.pid)
        return (
            process.is_running()
            and process.status() != psutil.STATUS_ZOMBIE
            and process.create_time() == identity.create_time
        )
    except psutil.Error:
        return False


def _service_connector_executable() -> Path:
    paths = runtime_paths(REPOSITORY_ROOT)
    executable = paths["python"].parent / "serena-light"
    assert executable.is_file()
    assert executable.resolve().is_relative_to(paths["runtime"].resolve())
    return executable


def _child_environment(runtime_root: Path, build_variant: str) -> dict[str, str]:
    environment = dict(os.environ)
    assert environment.get(cli.PYTEST_CURRENT_TEST_ENV)
    environment.update(
        {
            cli.ACCEPTANCE_RUNTIME_ROOT_ENV: str(runtime_root),
            cli.ACCEPTANCE_BUILD_VARIANT_ENV: build_variant,
            cli.ACCEPTANCE_WARM_GRACE_SECONDS_ENV: str(_WARM_GRACE_SECONDS),
            cli.ACCEPTANCE_IDLE_EXIT_SECONDS_ENV: str(_IDLE_EXIT_SECONDS),
        }
    )
    return environment


def _data(result: types.CallToolResult) -> Mapping[str, object]:
    payload = result.structuredContent
    assert result.isError is not True
    assert isinstance(payload, Mapping)
    assert payload.get("ok") is True
    data = payload.get("data")
    assert isinstance(data, Mapping)
    return cast(Mapping[str, object], data)


def _mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return cast(Mapping[str, object], value)


class _StdioClient:
    def __init__(self, *, workspace: Path, environment: Mapping[str, str]) -> None:
        self._workspace = workspace
        self._environment = dict(environment)
        self._commands: asyncio.Queue[
            tuple[Callable[[ClientSession], Awaitable[object]], asyncio.Future[object]] | None
        ] = asyncio.Queue()
        self._started: asyncio.Future[Mapping[str, object]] | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> Mapping[str, object]:
        assert self._task is None
        self._started = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._run())
        return await self._started

    async def status(self) -> Mapping[str, object]:
        result = await self._submit(lambda session: session.call_tool("get_runtime_status"))
        assert isinstance(result, types.CallToolResult)
        return _data(result)

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        await self._commands.put(None)
        await task

    async def _submit(self, callback: Callable[[ClientSession], Awaitable[object]]) -> object:
        assert self._task is not None
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        await self._commands.put((callback, future))
        return await future

    async def _run(self) -> None:
        assert self._started is not None
        try:
            async with AsyncExitStack() as stack:
                parameters = StdioServerParameters(
                    command=str(_service_connector_executable()),
                    args=[],
                    cwd=self._workspace,
                    env=self._environment,
                )
                read_stream, write_stream = await stack.enter_async_context(
                    stdio_client(parameters, errlog=sys.stderr)
                )
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "serena-light"
                listed = await session.list_tools()
                tool_names = {tool.name for tool in listed.tools}
                assert "get_runtime_status" in tool_names
                assert "replace_symbol_body" in tool_names
                self._started.set_result(_data(await session.call_tool("get_runtime_status")))
                while (command := await self._commands.get()) is not None:
                    callback, future = command
                    try:
                        future.set_result(await callback(session))
                    except BaseException as exc:
                        future.set_exception(exc)
        except BaseException as exc:
            if not self._started.done():
                self._started.set_exception(exc)
                return
            raise


def _initialize_git_workspace(path: Path) -> None:
    path.mkdir()
    completed = subprocess.run(
        ["git", "init", "--quiet", str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 0, completed.stderr


def _read_owned_daemon(build_root: Path) -> tuple[DiscoveryMetadata, _ProcessIdentity]:
    metadata = read_discovery_metadata(
        build_root,
        is_process_identity_live=lambda pid, created: _is_live(_ProcessIdentity(pid, created)),
    )
    identity = _ProcessIdentity(metadata.pid, metadata.process_start_time)
    assert _is_live(identity)
    process = psutil.Process(identity.pid)
    command = process.cmdline()
    service_python = runtime_paths(REPOSITORY_ROOT)["python"].resolve()
    assert Path(command[0]).resolve() == service_python
    assert command[1:] == ["-I", "-m", "serena_light.cli", "daemon"]
    return metadata, identity


def _migration_status(build_root: Path, metadata: DiscoveryMetadata) -> Mapping[str, object]:
    bearer = read_bearer_secret(build_root)
    response = httpx.get(
        metadata.endpoint.removesuffix("/mcp") + "/migration-status",
        headers={"Authorization": f"Bearer {bearer.value}"},
        timeout=1.0,
        trust_env=False,
    )
    response.raise_for_status()
    payload = response.json()
    assert isinstance(payload, Mapping) and payload.get("ok") is True
    return _mapping(payload.get("data"))


def _assert_binding(status: Mapping[str, object], *, build_identity: str, workspace: Path) -> None:
    assert _mapping(status["build"])["identity"] == build_identity
    binding = _mapping(status["workspace"])
    assert Path(cast(str, binding["root"])).resolve() == workspace.resolve()
    assert Path(cast(str, binding["working_subdirectory"])).resolve() == workspace.resolve()
    assert status["issues"] == []


async def _wait_for_retirement(
    *,
    build_root: Path,
    identity: _ProcessIdentity,
    zero_holders_at: float,
) -> None:
    deadline = time.monotonic() + 12.0
    while _is_live(identity) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not _is_live(identity), "test-owned zero-holder daemon did not retire"
    assert time.monotonic() >= zero_holders_at + _WARM_GRACE_SECONDS
    while (
        build_root.joinpath(DISCOVERY_NAME).exists() or build_root.joinpath(BEARER_NAME).exists()
    ) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert not build_root.joinpath(DISCOVERY_NAME).exists()
    assert not build_root.joinpath(BEARER_NAME).exists()


def _terminate_exact_test_daemon(identity: _ProcessIdentity) -> None:
    """Failure cleanup is restricted to a PID+create-time read from the isolated slot."""

    if not _is_live(identity):
        return
    os.kill(identity.pid, signal.SIGTERM)
    deadline = time.monotonic() + 5.0
    while _is_live(identity) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _is_live(identity):
        os.kill(identity.pid, signal.SIGKILL)
    deadline = time.monotonic() + 5.0
    while _is_live(identity) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _is_live(identity)


def test_real_service_connectors_roll_over_isolated_build_daemons_and_retire_after_grace(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runtime_root = (tmp_path / "isolated-runtime").resolve()
        assert runtime_root != RUNTIME_ROOT
        assert not runtime_root.is_relative_to(RUNTIME_ROOT)
        assert not RUNTIME_ROOT.is_relative_to(runtime_root)
        with pytest.raises(RuntimeFileError, match="must not overlap"):
            cli._acceptance_overrides(
                {
                    cli.PYTEST_CURRENT_TEST_ENV: "acceptance isolation guard",
                    cli.ACCEPTANCE_RUNTIME_ROOT_ENV: str(RUNTIME_ROOT),
                    cli.ACCEPTANCE_BUILD_VARIANT_ENV: _OLD_VARIANT,
                    cli.ACCEPTANCE_WARM_GRACE_SECONDS_ENV: str(_WARM_GRACE_SECONDS),
                    cli.ACCEPTANCE_IDLE_EXIT_SECONDS_ENV: str(_IDLE_EXIT_SECONDS),
                }
            )
        workspace_a = tmp_path / "workspace-a"
        workspace_b = tmp_path / "workspace-b"
        _initialize_git_workspace(workspace_a)
        _initialize_git_workspace(workspace_b)
        old_environment = _child_environment(runtime_root, _OLD_VARIANT)
        new_environment = _child_environment(runtime_root, _NEW_VARIANT)
        old_acceptance = cli._acceptance_overrides(old_environment)
        new_acceptance = cli._acceptance_overrides(new_environment)
        assert old_acceptance is not None
        assert new_acceptance is not None
        old_build = cli._acceptance_build_identity(old_acceptance)
        new_build = cli._acceptance_build_identity(new_acceptance)
        assert old_build != new_build
        old_root = prepare_runtime_layout(runtime_root, old_build).build_root
        new_root = prepare_runtime_layout(runtime_root, new_build).build_root

        old_a = _StdioClient(
            workspace=workspace_a,
            environment=old_environment,
        )
        old_b = _StdioClient(
            workspace=workspace_b,
            environment=old_environment,
        )
        new_b = _StdioClient(
            workspace=workspace_b,
            environment=new_environment,
        )
        clients = (old_a, old_b, new_b)
        owned: list[_ProcessIdentity] = []
        baseline_pids = {process.pid for process in psutil.process_iter()}
        try:
            old_a_status = await old_a.start()
            old_metadata, old_identity = _read_owned_daemon(old_root)
            owned.append(old_identity)
            assert old_identity.pid not in baseline_pids
            old_b_status = await old_b.start()
            assert _read_owned_daemon(old_root)[0].daemon_id == old_metadata.daemon_id
            assert _migration_status(old_root, old_metadata)["active_holders"] == 2

            new_b_status = await new_b.start()
            new_metadata, new_identity = _read_owned_daemon(new_root)
            owned.append(new_identity)
            assert new_identity.pid not in baseline_pids
            assert old_identity != new_identity
            assert old_metadata.daemon_id != new_metadata.daemon_id
            assert old_root != new_root
            assert old_metadata.build_identity == old_build
            assert new_metadata.build_identity == new_build

            _assert_binding(old_a_status, build_identity=old_build, workspace=workspace_a)
            _assert_binding(old_b_status, build_identity=old_build, workspace=workspace_b)
            _assert_binding(new_b_status, build_identity=new_build, workspace=workspace_b)
            _assert_binding(await old_a.status(), build_identity=old_build, workspace=workspace_a)
            _assert_binding(await old_b.status(), build_identity=old_build, workspace=workspace_b)
            assert _migration_status(old_root, old_metadata)["active_holders"] == 2
            assert _migration_status(new_root, new_metadata)["active_holders"] == 1
            assert _is_live(old_identity), "new-build startup must preserve the live old-build daemon"

            await old_a.aclose()
            assert _migration_status(old_root, old_metadata)["active_holders"] == 1
            await old_b.aclose()
            zero_holders_at = time.monotonic()
            old_zero = _migration_status(old_root, old_metadata)
            assert old_zero["active_holders"] == 0
            assert old_zero["daemon_idle"] is False
            await asyncio.sleep(_WARM_GRACE_SECONDS / 3)
            assert _is_live(old_identity), "old build retired before its workspace warm grace"
            assert _migration_status(old_root, old_metadata)["active_holders"] == 0

            await _wait_for_retirement(
                build_root=old_root,
                identity=old_identity,
                zero_holders_at=zero_holders_at,
            )
            _assert_binding(await new_b.status(), build_identity=new_build, workspace=workspace_b)
            assert _migration_status(new_root, new_metadata)["active_holders"] == 1

            await new_b.aclose()
            new_zero_holders_at = time.monotonic()
            new_zero = _migration_status(new_root, new_metadata)
            assert new_zero["active_holders"] == 0
            assert new_zero["daemon_idle"] is False
            await _wait_for_retirement(
                build_root=new_root,
                identity=new_identity,
                zero_holders_at=new_zero_holders_at,
            )
        finally:
            for client in reversed(clients):
                with suppress(BaseException):
                    await client.aclose()
            for identity in owned:
                _terminate_exact_test_daemon(identity)

    asyncio.run(scenario())
