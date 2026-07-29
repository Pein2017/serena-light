"""Real stdio acceptance for the repository-owned Serena Light connector."""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import AsyncExitStack, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

import httpx
import psutil
import pytest
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client

from serena_light import cli
from serena_light.bootstrap import inspect_runtime, runtime_paths
from serena_light.build_identity import compute_build_identity
from serena_light.runtime_files import (
    BearerSecret,
    DiscoveryMetadata,
    RuntimeFileError,
    prepare_runtime_layout,
    read_bearer_secret,
    read_discovery_metadata,
)

pytestmark = pytest.mark.timeout(75)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
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


@dataclass(frozen=True, slots=True)
class _DaemonProcess:
    daemon_id: str
    pid: int
    create_time: float


def _is_live(identity: _DaemonProcess) -> bool:
    try:
        process = psutil.Process(identity.pid)
        return (
            process.is_running()
            and process.status() != psutil.STATUS_ZOMBIE
            and process.create_time() == identity.create_time
        )
    except psutil.Error:
        return False


def _read_daemon(runtime_root: Path) -> DiscoveryMetadata | None:
    try:
        return read_discovery_metadata(
            runtime_root,
            is_process_identity_live=lambda pid, created: _is_live(
                _DaemonProcess(daemon_id="unneeded", pid=pid, create_time=created)
            ),
        )
    except RuntimeFileError:
        return None


def _discover_test_owned_daemon(
    runtime_root: Path,
    build_identity: str,
    *,
    expected_daemon_id: str | None = None,
) -> _DaemonProcess | None:
    """Recover only a live daemon registered in this isolated test build slot."""

    build_root = prepare_runtime_layout(runtime_root, build_identity).build_root
    metadata = _read_daemon(build_root)
    if metadata is None or metadata.build_identity != build_identity:
        return None
    if expected_daemon_id is not None and metadata.daemon_id != expected_daemon_id:
        return None
    owned = _DaemonProcess(metadata.daemon_id, metadata.pid, metadata.process_start_time)
    return owned if _is_live(owned) else None


def _service_connector_executable() -> Path:
    paths = runtime_paths(REPOSITORY_ROOT)
    executable = paths["python"].parent / "serena-light"
    assert executable.is_file(), "locked service runtime omitted the registered connector executable"
    assert executable.resolve().is_relative_to(paths["runtime"].resolve())
    return executable


def _migration_status(runtime_root: Path, metadata: DiscoveryMetadata) -> Mapping[str, object]:
    bearer: BearerSecret = read_bearer_secret(runtime_root)
    response = httpx.get(
        metadata.endpoint.removesuffix("/mcp") + "/migration-status",
        headers={"Authorization": f"Bearer {bearer.value}"},
        timeout=1.0,
        trust_env=False,
    )
    response.raise_for_status()
    payload = response.json()
    assert isinstance(payload, Mapping)
    assert payload.get("ok") is True
    data = payload.get("data")
    return _mapping(data)


def _mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return cast(Mapping[str, object], value)


def _terminate_owned_daemon(owned: _DaemonProcess, descendants: tuple[_DaemonProcess, ...]) -> None:
    """Reclaim only the exact detached process started by this test."""

    if _is_live(owned):
        os.kill(owned.pid, signal.SIGTERM)
    deadline = time.monotonic() + 5.0
    while _is_live(owned) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _is_live(owned):
        os.kill(owned.pid, signal.SIGKILL)
    deadline = time.monotonic() + 5.0
    while _is_live(owned) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _is_live(owned), "test-owned daemon did not terminate during teardown"
    assert not any(_is_live(child) for child in descendants), "test-owned daemon left a descendant behind"


def test_isolated_build_discovery_reclaims_only_the_exact_daemon_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_root = (tmp_path / "isolated-runtime").resolve()
    build_identity = "a" * 64
    daemon_id = str(uuid4())
    metadata = DiscoveryMetadata.create(
        daemon_id=daemon_id,
        pid=4321,
        process_start_time=123.0,
        endpoint="http://127.0.0.1:43123/mcp",
        protocol_version="1",
        server_version="test",
        created_at=124.0,
        build_identity=build_identity,
    )
    expected_root = prepare_runtime_layout(runtime_root, build_identity).build_root
    observed_roots: list[Path] = []

    def read(root: Path) -> DiscoveryMetadata | None:
        observed_roots.append(root)
        return metadata

    monkeypatch.setattr(sys.modules[__name__], "_read_daemon", read)
    monkeypatch.setattr(sys.modules[__name__], "_is_live", lambda identity: identity.pid == metadata.pid)

    assert _discover_test_owned_daemon(
        runtime_root,
        build_identity,
        expected_daemon_id=daemon_id,
    ) == _DaemonProcess(daemon_id, metadata.pid, metadata.process_start_time)
    assert observed_roots == [expected_root]
    assert _discover_test_owned_daemon(runtime_root, build_identity, expected_daemon_id=str(uuid4())) is None


@contextmanager
def _proxy_environment(monkeypatch: pytest.MonkeyPatch, *, poisoned: bool) -> Iterator[dict[str, str]]:
    """Return the exact environment explicitly passed to the stdio child."""

    with monkeypatch.context() as environment:
        for name in _PROXY_VARIABLES:
            environment.delenv(name, raising=False)
        if poisoned:
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                environment.setenv(name, "http://127.0.0.1:1")
            environment.setenv("NO_PROXY", "")
            environment.setenv("no_proxy", "")
        child_environment = dict(os.environ)
        if poisoned:
            assert child_environment["HTTP_PROXY"] == "http://127.0.0.1:1"
            assert child_environment["NO_PROXY"] == ""
        else:
            assert not any(name in child_environment for name in _PROXY_VARIABLES)
        yield child_environment


def _descendants(identity: _DaemonProcess) -> tuple[_DaemonProcess, ...]:
    if not _is_live(identity):
        return ()
    return tuple(
        _DaemonProcess(f"descendant:{child.pid}", child.pid, child.create_time())
        for child in psutil.Process(identity.pid).children(recursive=True)
        if child.status() != psutil.STATUS_ZOMBIE
    )


def _data(result: types.CallToolResult) -> Mapping[str, object]:
    payload = result.structuredContent
    assert result.isError is not True
    assert isinstance(payload, Mapping)
    assert payload.get("ok") is True
    data = payload.get("data")
    assert isinstance(data, Mapping)
    return cast(Mapping[str, object], data)


async def _run_fresh_stdio_client(
    workspace: Path,
    *,
    child_environment: Mapping[str, str],
    release_workspace: bool,
    expected_workspace_root: Path,
    edit_source: Path | None = None,
    expected_build_identity: str | None = None,
    immediate_release: bool = False,
) -> str:
    parameters = StdioServerParameters(
        command=str(_service_connector_executable()),
        args=[],
        cwd=workspace,
        env=dict(child_environment),
    )
    assert parameters.env == child_environment
    async with stdio_client(parameters, errlog=sys.stderr) as (read_stream, write_stream), ClientSession(
        read_stream, write_stream
    ) as client:
        initialized = await client.initialize()
        assert initialized.serverInfo.name == "serena-light"

        listed = await client.list_tools()
        tool_names = {tool.name for tool in listed.tools}
        assert "get_runtime_status" in tool_names
        assert "replace_symbol_body" in tool_names

        status = _data(await client.call_tool("get_runtime_status"))
        assert status["build_identity"] == (
            compute_build_identity(REPOSITORY_ROOT)
            if expected_build_identity is None
            else expected_build_identity
        )
        binding = _mapping(status["binding"])
        assert Path(cast(str, binding["working_subdirectory"])).resolve() == workspace
        runtime = _mapping(status["runtime"])
        identity = _mapping(runtime["identity"])
        assert Path(cast(str, identity["root"])).resolve() == expected_workspace_root
        if edit_source is not None:
            original = edit_source.read_bytes()
            edited = _data(
                await client.call_tool(
                    "replace_symbol_body",
                    {
                        "name_path": "target",
                        "relative_path": edit_source.name,
                        "body": "def target() -> int:\n    return 2",
                        "expected_hash": hashlib.sha256(original).hexdigest(),
                    },
                )
            )
            assert edited["relative_path"] == edit_source.name
            assert edit_source.read_text() == "def target() -> int:\n    return 2\n"
        daemon_id = status["daemon_id"]
        assert isinstance(daemon_id, str)
        if release_workspace:
            arguments = {"immediate": True} if immediate_release else None
            released = _data(await client.call_tool("release_workspace", arguments))
            assert released["bound"] is False
            assert released["immediate"] is immediate_release
        return daemon_id


class _HeldStdioClient:
    """A test-owned stdio client whose lease remains active until explicitly closed."""

    def __init__(
        self,
        workspace: Path,
        *,
        child_environment: Mapping[str, str],
        expected_workspace_root: Path,
        expected_build_identity: str,
    ) -> None:
        self._workspace = workspace
        self._child_environment = dict(child_environment)
        self._expected_workspace_root = expected_workspace_root
        self._expected_build_identity = expected_build_identity
        self._closed = asyncio.Queue[None]()
        self._started: asyncio.Future[str] | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> str:
        assert self._task is None
        self._started = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._run())
        return await self._started

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        await self._closed.put(None)
        await task

    async def _run(self) -> None:
        assert self._started is not None
        try:
            async with AsyncExitStack() as stack:
                parameters = StdioServerParameters(
                    command=str(_service_connector_executable()),
                    args=[],
                    cwd=self._workspace,
                    env=self._child_environment,
                )
                read_stream, write_stream = await stack.enter_async_context(stdio_client(parameters, errlog=sys.stderr))
                client = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                initialized = await client.initialize()
                assert initialized.serverInfo.name == "serena-light"
                status = _data(await client.call_tool("get_runtime_status"))
                assert status["build_identity"] == self._expected_build_identity
                binding = _mapping(status["binding"])
                assert Path(cast(str, binding["working_subdirectory"])).resolve() == self._workspace
                runtime = _mapping(status["runtime"])
                identity = _mapping(runtime["identity"])
                assert Path(cast(str, identity["root"])).resolve() == self._expected_workspace_root
                daemon_id = status["daemon_id"]
                assert isinstance(daemon_id, str)
                self._started.set_result(daemon_id)
                await self._closed.get()
        except BaseException as exc:
            if not self._started.done():
                self._started.set_exception(exc)
                return
            raise


def test_real_stdio_connector_contains_proxy_environment_and_releases_to_warm_grace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Isolated clean and poisoned clients retain one test-owned warm daemon."""

    runtime = inspect_runtime(REPOSITORY_ROOT)
    assert Path(cast(str, runtime["runtime"])).resolve() == runtime_paths(REPOSITORY_ROOT)["runtime"].resolve()
    _service_connector_executable()
    runtime_root = (tmp_path / "isolated-proxy-runtime").resolve()
    variant = "isolatedProxyWarmGrace1"

    async def scenario() -> None:
        clients: list[_HeldStdioClient] = []
        owned: _DaemonProcess | None = None
        descendants: tuple[_DaemonProcess, ...] = ()
        first_daemon_id: str | None = None
        build_identity: str | None = None
        try:
            with _proxy_environment(monkeypatch, poisoned=False) as clean_environment:
                clean_environment.update(
                    {
                        cli.ACCEPTANCE_RUNTIME_ROOT_ENV: str(runtime_root),
                        cli.ACCEPTANCE_BUILD_VARIANT_ENV: variant,
                        cli.ACCEPTANCE_WARM_GRACE_SECONDS_ENV: "1",
                        cli.ACCEPTANCE_IDLE_EXIT_SECONDS_ENV: "8",
                    }
                )
                acceptance = cli._acceptance_overrides(clean_environment)
                assert acceptance is not None
                build_identity = cli._acceptance_build_identity(acceptance)
                build_root = prepare_runtime_layout(runtime_root, build_identity).build_root
                first = _HeldStdioClient(
                    REPOSITORY_ROOT,
                    child_environment=clean_environment,
                    expected_workspace_root=REPOSITORY_ROOT,
                    expected_build_identity=build_identity,
                )
                # Startup includes initialization and status validation.  Register the
                # holder before awaiting it so a detached daemon remains in teardown
                # ownership if either validation step fails.
                clients.append(first)
                first_daemon_id = await first.start()
            metadata = _read_daemon(build_root)
            assert metadata is not None and metadata.daemon_id == first_daemon_id
            owned = _DaemonProcess(metadata.daemon_id, metadata.pid, metadata.process_start_time)
            assert _is_live(owned)
            descendants = _descendants(owned)
            assert _migration_status(build_root, metadata)["active_holders"] == 1

            with _proxy_environment(monkeypatch, poisoned=True) as poisoned_environment:
                poisoned_environment.update(
                    {
                        cli.ACCEPTANCE_RUNTIME_ROOT_ENV: str(runtime_root),
                        cli.ACCEPTANCE_BUILD_VARIANT_ENV: variant,
                        cli.ACCEPTANCE_WARM_GRACE_SECONDS_ENV: "1",
                        cli.ACCEPTANCE_IDLE_EXIT_SECONDS_ENV: "8",
                    }
                )
                second = _HeldStdioClient(
                    REPOSITORY_ROOT / "src",
                    child_environment=poisoned_environment,
                    expected_workspace_root=REPOSITORY_ROOT,
                    expected_build_identity=build_identity,
                )
                second_daemon_id = await second.start()
            clients.append(second)
            assert second_daemon_id == first_daemon_id
            assert _migration_status(build_root, metadata)["active_holders"] == 2
            assert _is_live(owned)

            await first.aclose()
            clients.remove(first)
            assert _migration_status(build_root, metadata)["active_holders"] == 1
            assert _is_live(owned), "closing one test-owned holder must preserve the second holder's daemon"
            assert _descendants(owned) == descendants
        finally:
            for client in reversed(clients):
                with suppress(BaseException):
                    await client.aclose()
            if owned is None and build_identity is not None:
                # This root is a tmp_path-owned acceptance slot, never the canonical
                # runtime.  Discovery must still match its build identity and, once
                # available, the daemon UUID before its PID/create-time is reclaimed.
                owned = _discover_test_owned_daemon(
                    runtime_root,
                    build_identity,
                    expected_daemon_id=first_daemon_id,
                )
            if owned is not None:
                _terminate_owned_daemon(owned, descendants or _descendants(owned))

    asyncio.run(scenario())


def test_real_stdio_connector_performs_hash_edit_and_release_under_poisoned_proxy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The restored public edit crosses the real connector/daemon boundary exactly once."""

    runtime_root = (tmp_path / "isolated-edit-runtime").resolve()
    edit_workspace = Path(tempfile.mkdtemp(prefix="serena-light-edit-", dir="/data"))
    edit_source = edit_workspace / "example.py"
    nested_cwd = edit_workspace / "nested"
    build_root: Path | None = None
    try:
        completed = subprocess.run(
            ["git", "init", "--quiet", str(edit_workspace)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert completed.returncode == 0, completed.stderr
        (edit_workspace / "pyrightconfig.json").write_text('{"include":["example.py"]}\n')
        edit_source.write_text("def target() -> int:\n    return 1\n")
        nested_cwd.mkdir()

        with _proxy_environment(monkeypatch, poisoned=True) as child_environment:
            child_environment.update(
                {
                    cli.ACCEPTANCE_RUNTIME_ROOT_ENV: str(runtime_root),
                    cli.ACCEPTANCE_BUILD_VARIANT_ENV: "hashEditRestored1",
                    cli.ACCEPTANCE_WARM_GRACE_SECONDS_ENV: "1",
                    cli.ACCEPTANCE_IDLE_EXIT_SECONDS_ENV: "8",
                }
            )
            acceptance = cli._acceptance_overrides(child_environment)
            assert acceptance is not None
            build_identity = cli._acceptance_build_identity(acceptance)
            build_root = prepare_runtime_layout(runtime_root, build_identity).build_root
            asyncio.run(
                _run_fresh_stdio_client(
                    nested_cwd,
                    child_environment=child_environment,
                    release_workspace=True,
                    expected_workspace_root=edit_workspace,
                    edit_source=edit_source,
                    expected_build_identity=build_identity,
                    immediate_release=True,
                )
            )
    finally:
        if build_root is not None and (metadata := _read_daemon(build_root)) is not None:
            owned = _DaemonProcess(metadata.daemon_id, metadata.pid, metadata.process_start_time)
            _terminate_owned_daemon(owned, _descendants(owned))
        shutil.rmtree(edit_workspace)
