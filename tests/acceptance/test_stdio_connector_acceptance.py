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
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast

import httpx
import psutil
import pytest
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client

from serena_light import cli
from serena_light.bootstrap import inspect_runtime, runtime_paths
from serena_light.build_identity import compute_build_identity
from serena_light.runtime_files import (
    RUNTIME_ROOT,
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


def _current_runtime_root() -> Path:
    return prepare_runtime_layout(RUNTIME_ROOT, compute_build_identity(REPOSITORY_ROOT)).build_root


def _service_connector_executable() -> Path:
    paths = runtime_paths(REPOSITORY_ROOT)
    executable = paths["python"].parent / "serena-light"
    assert executable.is_file(), "locked service runtime omitted the registered connector executable"
    assert executable.resolve().is_relative_to(paths["runtime"].resolve())
    return executable


def _wait_for_unowned_daemon_to_retire(runtime_root: Path) -> None:
    """Avoid taking ownership of a daemon that another interactive client holds."""

    deadline = time.monotonic() + cli.DAEMON_IDLE_EXIT_SECONDS + 3.0
    while _read_daemon(runtime_root) is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    if (metadata := _read_daemon(runtime_root)) is not None:
        reason = (
            "shared serena-light daemon is already live "
            f"(pid={metadata.pid}, daemon_id={metadata.daemon_id}); refusing to disrupt another client"
        )
        skip = cast(Callable[[str], NoReturn], pytest.skip)
        skip(reason)


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


@contextmanager
def _proxy_environment(monkeypatch: pytest.MonkeyPatch, *, poisoned: bool) -> Iterator[None]:
    with monkeypatch.context() as environment:
        for name in _PROXY_VARIABLES:
            environment.delenv(name, raising=False)
        if poisoned:
            for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                environment.setenv(name, "http://127.0.0.1:1")
            environment.setenv("NO_PROXY", "")
            environment.setenv("no_proxy", "")
        yield


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
    release_workspace: bool,
    edit_source: Path | None = None,
) -> str:
    parameters = StdioServerParameters(
        command=str(_service_connector_executable()),
        args=[],
        cwd=workspace,
    )
    async with stdio_client(parameters, errlog=sys.stderr) as (read_stream, write_stream), ClientSession(
        read_stream, write_stream
    ) as client:
        initialized = await client.initialize()
        assert initialized.serverInfo.name == "serena-light"

        listed = await client.list_tools()
        tool_names = {tool.name for tool in listed.tools}
        assert "get_runtime_status" in tool_names
        assert "replace_symbol_body" not in tool_names

        status = _data(await client.call_tool("get_runtime_status"))
        assert status["build_identity"] == compute_build_identity(REPOSITORY_ROOT)
        binding = _mapping(status["binding"])
        assert Path(cast(str, binding["working_subdirectory"])).resolve() == workspace
        runtime = _mapping(status["runtime"])
        identity = _mapping(runtime["identity"])
        assert Path(cast(str, identity["root"])).resolve() == workspace
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
            released = _data(await client.call_tool("release_workspace"))
            assert released["bound"] is False
        return daemon_id


def test_real_stdio_connector_contains_proxy_environment_edits_and_releases_to_warm_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clean and poisoned clients must not route loopback MCP through ambient proxies."""

    runtime = inspect_runtime(REPOSITORY_ROOT)
    assert Path(cast(str, runtime["runtime"])).resolve() == runtime_paths(REPOSITORY_ROOT)["runtime"].resolve()
    _service_connector_executable()
    runtime_root = _current_runtime_root()
    _wait_for_unowned_daemon_to_retire(runtime_root)

    daemon_ids: list[str] = []
    owned: _DaemonProcess | None = None
    descendants: tuple[_DaemonProcess, ...] = ()
    edit_workspace = Path(tempfile.mkdtemp(prefix="serena-light-edit-", dir="/data"))
    edit_source = edit_workspace / "example.py"
    subprocess.run(["git", "init", "-q"], cwd=edit_workspace, check=True)
    (edit_workspace / "pyrightconfig.json").write_text('{"include":["example.py"]}\n')
    edit_source.write_text("def target() -> int:\n    return 1\n")
    try:
        for poisoned in (False, True):
            with _proxy_environment(monkeypatch, poisoned=poisoned):
                daemon_id = asyncio.run(
                    _run_fresh_stdio_client(
                        edit_workspace if poisoned else REPOSITORY_ROOT,
                        release_workspace=poisoned,
                        edit_source=None,
                    )
                )
            daemon_ids.append(daemon_id)

            metadata = _read_daemon(runtime_root)
            assert metadata is not None
            assert metadata.daemon_id == daemon_id
            candidate = _DaemonProcess(metadata.daemon_id, metadata.pid, metadata.process_start_time)
            assert _is_live(candidate)
            lifetime = _migration_status(runtime_root, metadata)
            assert lifetime["active_holders"] == 0
            assert lifetime["daemon_idle"] is False
            if owned is None:
                owned = candidate
                descendants = tuple(
                    _DaemonProcess(f"descendant:{child.pid}", child.pid, child.create_time())
                    for child in psutil.Process(candidate.pid).children(recursive=True)
                    if child.status() != psutil.STATUS_ZOMBIE
                )
    finally:
        if owned is not None:
            _terminate_owned_daemon(owned, descendants)
        shutil.rmtree(edit_workspace)

    assert daemon_ids[0] == daemon_ids[1], "the second fresh stdio client should reuse the warm service daemon"
