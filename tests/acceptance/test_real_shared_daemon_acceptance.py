"""Real connector/daemon acceptance for concurrent leases on one shared build daemon.

Versioned rollover between distinct build identities is owned by
``test_real_versioned_rollover_acceptance.py``.  This module owns the single-build
runtime claims: concurrent clients on one root, a second root coexisting on the same
daemon, explicit cross-root and same-root ``activate_workspace``, partial lease
release, and reclamation of the exact test-owned daemon and language-server
descendants.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import AsyncExitStack, contextmanager, suppress
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
    prepare_runtime_layout,
    read_bearer_secret,
    read_discovery_metadata,
)

pytestmark = pytest.mark.timeout(180)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_BUILD_VARIANT = "sharedDaemonLeases1"
_WARM_GRACE_SECONDS = 1.0
_IDLE_EXIT_SECONDS = 0.5
_RETIREMENT_TIMEOUT_SECONDS = 25.0
_UNROUTABLE_PROXY = "http://127.0.0.1:1"
_PROXY_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)
_ROUTED_PROXY_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


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


def _descendants(identity: _ProcessIdentity) -> tuple[_ProcessIdentity, ...]:
    if not _is_live(identity):
        return ()
    try:
        children = psutil.Process(identity.pid).children(recursive=True)
    except psutil.Error:
        return ()
    descendants: list[_ProcessIdentity] = []
    for child in children:
        try:
            if child.status() != psutil.STATUS_ZOMBIE:
                descendants.append(_ProcessIdentity(child.pid, child.create_time()))
        except psutil.Error:
            continue
    return tuple(descendants)


def _service_connector_executable() -> Path:
    paths = runtime_paths(REPOSITORY_ROOT)
    executable = paths["python"].parent / "serena-light"
    assert executable.is_file(), "locked service runtime omitted the registered connector executable"
    assert executable.resolve().is_relative_to(paths["runtime"].resolve())
    return executable


@contextmanager
def _poisoned_ambient_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Poison every inherited proxy variable so loopback traffic must stay direct."""

    with monkeypatch.context() as environment:
        for name in _ROUTED_PROXY_VARIABLES:
            environment.setenv(name, _UNROUTABLE_PROXY)
        environment.setenv("NO_PROXY", "")
        environment.setenv("no_proxy", "")
        yield


def _acceptance_environment(base: Mapping[str, str], runtime_root: Path) -> dict[str, str]:
    environment = dict(base)
    assert environment.get(cli.PYTEST_CURRENT_TEST_ENV)
    environment.update(
        {
            cli.ACCEPTANCE_RUNTIME_ROOT_ENV: str(runtime_root),
            cli.ACCEPTANCE_BUILD_VARIANT_ENV: _BUILD_VARIANT,
            cli.ACCEPTANCE_WARM_GRACE_SECONDS_ENV: str(_WARM_GRACE_SECONDS),
            cli.ACCEPTANCE_IDLE_EXIT_SECONDS_ENV: str(_IDLE_EXIT_SECONDS),
        }
    )
    return environment


def _mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return cast(Mapping[str, object], value)


def _data(result: types.CallToolResult) -> Mapping[str, object]:
    payload = result.structuredContent
    assert result.isError is not True, result
    assert isinstance(payload, Mapping)
    assert payload.get("ok") is True, payload
    return _mapping(payload.get("data"))


class _HeldStdioClient:
    """A test-owned stdio client whose daemon lease stays held until it is closed."""

    def __init__(self, *, cwd: Path, environment: Mapping[str, str]) -> None:
        self._cwd = cwd
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
        return _data(await self._call("get_runtime_status", None))

    async def activate(self, workspace: Path) -> Mapping[str, object]:
        return _data(await self._call("activate_workspace", {"absolute_path": str(workspace)}))

    async def overview(self, relative_path: str) -> Mapping[str, object]:
        return _data(
            await self._call(
                "get_symbols_overview",
                {"relative_path": relative_path, "max_depth": 1, "max_answer_chars": 12_000},
            )
        )

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        await self._commands.put(None)
        await task

    async def _call(self, name: str, arguments: Mapping[str, object] | None) -> types.CallToolResult:
        result = await self._submit(
            lambda session: session.call_tool(name, None if arguments is None else dict(arguments))
        )
        assert isinstance(result, types.CallToolResult)
        return result

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
                    cwd=self._cwd,
                    env=self._environment,
                )
                read_stream, write_stream = await stack.enter_async_context(stdio_client(parameters, errlog=sys.stderr))
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "serena-light"
                listed = await session.list_tools()
                tool_names = {tool.name for tool in listed.tools}
                assert {"get_runtime_status", "activate_workspace", "release_workspace"} <= tool_names
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


def _initialize_git_workspace(path: Path, *, module: str) -> None:
    """Build a deterministic temporary Git root so no real source root is mutated."""

    path.mkdir()
    completed = subprocess.run(
        ["git", "init", "--quiet", str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 0, completed.stderr
    (path / "pyrightconfig.json").write_text('{"include":["example.py"]}\n')
    (path / "example.py").write_text(f"MODULE = {module!r}\n\n\ndef target() -> int:\n    return 1\n")


def _read_owned_daemon(build_root: Path) -> tuple[DiscoveryMetadata, _ProcessIdentity]:
    metadata = read_discovery_metadata(
        build_root,
        is_process_identity_live=lambda pid, created: _is_live(_ProcessIdentity(pid, created)),
    )
    identity = _ProcessIdentity(metadata.pid, metadata.process_start_time)
    assert _is_live(identity)
    command = psutil.Process(identity.pid).cmdline()
    service_python = runtime_paths(REPOSITORY_ROOT)["python"].resolve()
    assert Path(command[0]).resolve() == service_python
    assert command[1:] == ["-I", "-m", "serena_light.cli", "daemon"]
    return metadata, identity


def _migration_status(build_root: Path, metadata: DiscoveryMetadata) -> Mapping[str, object]:
    bearer = read_bearer_secret(build_root)
    response = httpx.get(
        metadata.endpoint.removesuffix("/mcp") + "/migration-status",
        headers={"Authorization": f"Bearer {bearer.value}"},
        timeout=2.0,
        trust_env=False,
    )
    response.raise_for_status()
    payload = response.json()
    assert isinstance(payload, Mapping) and payload.get("ok") is True
    return _mapping(payload.get("data"))


def _active_holders(build_root: Path, metadata: DiscoveryMetadata) -> int:
    holders = _migration_status(build_root, metadata)["active_holders"]
    assert isinstance(holders, int)
    return holders


def _assert_bound_to(status: Mapping[str, object], *, build_identity: str, workspace: Path) -> None:
    assert status["build_identity"] == build_identity
    binding = _mapping(status["binding"])
    assert Path(cast(str, binding["identity"])).resolve() == workspace
    runtime_identity = _mapping(_mapping(status["runtime"])["identity"])
    assert Path(cast(str, runtime_identity["root"])).resolve() == workspace


async def _wait_until(predicate: Callable[[], bool], *, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert predicate(), message


def _terminate_exact_identity(identity: _ProcessIdentity) -> None:
    """Reclaim one PID+create-time pair recorded from this test's isolated slot."""

    if not _is_live(identity):
        return
    with suppress(ProcessLookupError):
        os.kill(identity.pid, signal.SIGTERM)
    deadline = time.monotonic() + 5.0
    while _is_live(identity) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _is_live(identity):
        with suppress(ProcessLookupError):
            os.kill(identity.pid, signal.SIGKILL)
    deadline = time.monotonic() + 5.0
    while _is_live(identity) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _is_live(identity)


def test_real_shared_daemon_serves_concurrent_roots_and_survives_partial_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Three isolated clients share one daemon across two roots without losing it early."""

    runtime_root = (tmp_path / "isolated-shared-runtime").resolve()
    assert not runtime_root.is_relative_to(RUNTIME_ROOT)
    assert not RUNTIME_ROOT.is_relative_to(runtime_root)
    shared_root = (tmp_path / "shared-root").resolve()
    other_root = (tmp_path / "other-root").resolve()
    _initialize_git_workspace(shared_root, module="shared")
    _initialize_git_workspace(other_root, module="other")
    nested = shared_root / "nested"
    nested.mkdir()

    async def scenario(clean: Mapping[str, str], poisoned: Mapping[str, str]) -> None:
        acceptance = cli._acceptance_overrides(clean)
        assert acceptance is not None
        build_identity = cli._acceptance_build_identity(acceptance)
        layout = prepare_runtime_layout(runtime_root, build_identity)
        build_root = layout.build_root

        shared_a = _HeldStdioClient(cwd=shared_root, environment=clean)
        shared_b = _HeldStdioClient(cwd=nested, environment=poisoned)
        other_c = _HeldStdioClient(cwd=other_root, environment=clean)
        clients = [shared_a, shared_b, other_c]
        daemon: _ProcessIdentity | None = None
        descendants: tuple[_ProcessIdentity, ...] = ()
        baseline_pids = {process.pid for process in psutil.process_iter()}
        try:
            status_a = await shared_a.start()
            metadata, daemon = _read_owned_daemon(build_root)
            assert daemon.pid not in baseline_pids
            status_b = await shared_b.start()
            status_c = await other_c.start()

            # 1) Concurrent clients lease the same daemon and the same workspace root.
            assert status_a["daemon_id"] == metadata.daemon_id
            assert status_b["daemon_id"] == metadata.daemon_id
            _assert_bound_to(status_a, build_identity=build_identity, workspace=shared_root)
            _assert_bound_to(status_b, build_identity=build_identity, workspace=shared_root)
            assert Path(cast(str, _mapping(status_b["binding"])["working_subdirectory"])).resolve() == nested

            # 2) A second root coexists on that same daemon.
            assert status_c["daemon_id"] == metadata.daemon_id
            _assert_bound_to(status_c, build_identity=build_identity, workspace=other_root)
            assert _active_holders(build_root, metadata) == 3
            assert _read_owned_daemon(build_root)[1] == daemon

            for client, root in ((shared_a, shared_root), (other_c, other_root)):
                overview = await client.overview("example.py")
                assert overview["workspace"] == str(root)
                assert overview["files"], overview
            descendants = _descendants(daemon)
            assert descendants, "shared daemon started no language-server descendant to account for"

            # 3) One client switches roots explicitly, then reactivates the same root.
            switched = await other_c.activate(shared_root)
            assert Path(cast(str, _mapping(switched["workspace"])["identity"])).resolve() == shared_root
            _assert_bound_to(await other_c.status(), build_identity=build_identity, workspace=shared_root)
            reactivated = await other_c.activate(shared_root)
            assert Path(cast(str, _mapping(reactivated["workspace"])["identity"])).resolve() == shared_root
            _assert_bound_to(await other_c.status(), build_identity=build_identity, workspace=shared_root)
            assert _active_holders(build_root, metadata) == 3
            assert _read_owned_daemon(build_root)[1] == daemon, "activation must not replace the daemon"
            assert [entry.name for entry in layout.builds_root.iterdir()] == [build_identity]

            # 4) Releasing one lease keeps a daemon that another client still holds.
            await shared_a.aclose()
            clients.remove(shared_a)
            assert _active_holders(build_root, metadata) == 2
            await asyncio.sleep(_WARM_GRACE_SECONDS * 2)
            assert _is_live(daemon), "daemon retired while two clients still held leases"
            assert _active_holders(build_root, metadata) == 2
            _assert_bound_to(await shared_b.status(), build_identity=build_identity, workspace=shared_root)

            await shared_b.aclose()
            clients.remove(shared_b)
            assert _active_holders(build_root, metadata) == 1
            await asyncio.sleep(_WARM_GRACE_SECONDS * 2)
            assert _is_live(daemon), "daemon retired while the last client still held its lease"
            _assert_bound_to(await other_c.status(), build_identity=build_identity, workspace=shared_root)

            # 5) Only a zero-holder daemon retires, taking its language servers with it.
            await other_c.aclose()
            clients.remove(other_c)
            assert _active_holders(build_root, metadata) == 0
            await _wait_until(
                lambda: not _is_live(daemon),
                timeout=_RETIREMENT_TIMEOUT_SECONDS,
                message="test-owned zero-holder daemon did not retire",
            )
            surviving = tuple(child for child in descendants if _is_live(child))
            assert not surviving, f"retired daemon orphaned language-server descendants: {surviving}"
            assert not build_root.joinpath(DISCOVERY_NAME).exists()
            assert not build_root.joinpath(BEARER_NAME).exists()
        finally:
            for client in reversed(clients):
                with suppress(BaseException):
                    await client.aclose()
            if daemon is not None:
                for child in _descendants(daemon):
                    _terminate_exact_identity(child)
                _terminate_exact_identity(daemon)
            for child in descendants:
                _terminate_exact_identity(child)

    with _poisoned_ambient_environment(monkeypatch):
        clean = _acceptance_environment(
            {name: value for name, value in os.environ.items() if name not in _PROXY_VARIABLES},
            runtime_root,
        )
        poisoned = _acceptance_environment(os.environ, runtime_root)
        assert not any(name in clean for name in _PROXY_VARIABLES)
        assert poisoned["HTTP_PROXY"] == _UNROUTABLE_PROXY and poisoned["NO_PROXY"] == ""
        asyncio.run(scenario(clean, poisoned))
