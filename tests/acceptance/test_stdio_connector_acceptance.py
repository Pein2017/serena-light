"""Real stdio acceptance for the repository-owned Serena Light connector."""

from __future__ import annotations

import asyncio
import hashlib
import json
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
from serena_light.instructions import AGENT_INSTRUCTIONS
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
    survivors = [child for child in descendants if _is_live(child)]
    for child in survivors:
        os.kill(child.pid, signal.SIGTERM)
    deadline = time.monotonic() + 5.0
    while any(_is_live(child) for child in survivors) and time.monotonic() < deadline:
        time.sleep(0.05)
    for child in survivors:
        if _is_live(child):
            os.kill(child.pid, signal.SIGKILL)
    deadline = time.monotonic() + 5.0
    while any(_is_live(child) for child in survivors) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not any(_is_live(child) for child in survivors), "test-owned daemon left a descendant behind"


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
        assert initialized.instructions == AGENT_INSTRUCTIONS
        assert len(initialized.instructions.encode()) <= 220

        listed = await client.list_tools()
        tool_names = {tool.name for tool in listed.tools}
        assert len(listed.tools) == 11
        assert "get_runtime_status" in tool_names
        assert "replace_symbol_body" in tool_names
        descriptions = {tool.name: (tool.description or "") for tool in listed.tools}
        assert "startup cwd is auto-bound" in descriptions["activate_workspace"]
        assert "Shell cd does not change this lease" in descriptions["activate_workspace"]
        assert "depth 0" in descriptions["get_symbols_overview"]
        assert "qualified name path" in descriptions["find_symbol"]
        assert "snippets are opt-in" in descriptions["find_referencing_symbols"]
        assert "meaningful edit group" in descriptions["get_diagnostics_for_file"]
        assert "not routine preflight" in descriptions["get_runtime_status"]

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


async def _run_compact_navigation_stdio_client(
    workspace: Path,
    *,
    child_environment: Mapping[str, str],
    expected_build_identity: str,
    fixture_root: str,
) -> str:
    """Exercise every compact navigation tool through a real stdio connector."""

    parameters = StdioServerParameters(
        command=str(_service_connector_executable()),
        args=[],
        cwd=workspace,
        env=dict(child_environment),
    )
    prefix = f"{fixture_root}/" if fixture_root else ""
    cases: tuple[tuple[str, Mapping[str, object]], ...] = (
        (
            "get_symbols_overview",
            {"relative_path": f"{prefix}large_nested.py", "max_depth": 1, "max_answer_chars": 12_000},
        ),
        (
            "find_symbol",
            {
                "relative_path": f"{prefix}python_symbols.py",
                "name_path": "ANSWER",
                "include_body": True,
                "max_answer_chars": 12_000,
            },
        ),
        (
            "find_symbol",
            {
                "relative_path": fixture_root or ".",
                "name_path": "ANSWER",
                "max_matches": 20,
                "max_answer_chars": 12_000,
            },
        ),
        (
            "find_referencing_symbols",
            {
                "relative_path": f"{prefix}python_symbols.py",
                "name_path": "ANSWER",
                "max_answer_chars": 12_000,
            },
        ),
        (
            "find_declaration",
            {
                "relative_path": f"{prefix}python_usage.py",
                "regex": r"import (ANSWER), Calculator",
                "max_answer_chars": 12_000,
            },
        ),
        (
            "find_implementations",
            {
                "relative_path": f"{prefix}typescript_symbols.ts",
                "name_path": "Runner",
                "max_answer_chars": 12_000,
            },
        ),
        (
            "find_declaration",
            {
                "relative_path": f"{prefix}python_symbols.py",
                "regex": r"return (GenerationConfig)\(",
                "max_answer_chars": 12_000,
            },
        ),
        (
            "get_symbols_overview",
            {"relative_path": f"{prefix}empty.py", "max_depth": 1, "max_answer_chars": 12_000},
        ),
    )
    async with stdio_client(parameters, errlog=sys.stderr) as (read_stream, write_stream), ClientSession(
        read_stream, write_stream
    ) as client:
        initialized = await client.initialize()
        assert initialized.serverInfo.name == "serena-light"
        status = _data(await client.call_tool("get_runtime_status"))
        assert status["build_identity"] == expected_build_identity
        daemon_id = status["daemon_id"]
        assert isinstance(daemon_id, str)
        observed: list[Mapping[str, object]] = []
        for tool, arguments in cases:
            result = await client.call_tool(tool, dict(arguments))
            assert result.isError is not True, (tool, result)
            assert len(result.content) == 1
            block = result.content[0]
            assert isinstance(block, types.TextContent)
            assert result.structuredContent is not None
            assert block.text == json.dumps(
                result.structuredContent,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            assert len(block.text) <= cast(int, arguments["max_answer_chars"])
            data = _data(result)
            assert data["workspace"] == str(workspace)
            assert isinstance(data["files"], list)
            assert isinstance(data["omitted"], int)
            observed.append(data)

        (
            overview,
            exact_body,
            directory_symbols,
            references,
            declaration,
            implementation,
            external,
            empty,
        ) = observed
        assert overview["files"] and overview["omitted"] == 0
        body_file = cast(list[Mapping[str, object]], exact_body["files"])[0]
        fixture_hash = hashlib.sha256((workspace / f"{prefix}python_symbols.py").read_bytes()).hexdigest()
        assert body_file["sha256"] == fixture_hash
        assert cast(list[Mapping[str, object]], body_file["symbols"])[0]["body"] == "ANSWER: int = 42"
        scoped_files = cast(list[Mapping[str, object]], directory_symbols["files"])
        assert len(scoped_files) == 2
        assert {file["language"] for file in scoped_files} == {"python", "typescript"}
        assert all(
            cast(list[Mapping[str, object]], file["symbols"])[0]["name_path"] == "ANSWER"
            for file in scoped_files
        )
        coverage = cast(Mapping[str, object], references["coverage"])
        assert coverage == {
            "complete": False,
            "uncovered_files": 1,
            "sample": [
                {
                    "path": "python_uncovered.py",
                    "reason": "excluded_by_native_config",
                }
            ],
            "omitted": 0,
        }
        reference_files = cast(list[Mapping[str, object]], references["files"])
        assert [file["path"] for file in reference_files] == [f"{prefix}python_usage.py"]
        assert all(file["path"] != f"{prefix}python_symbols.py" for file in reference_files)
        assert cast(list[Mapping[str, object]], declaration["files"])[0]["targets"]
        assert cast(list[Mapping[str, object]], implementation["files"])[0]["targets"]
        external_file = cast(list[Mapping[str, object]], external["files"])[0]
        assert external_file["read_only"] is True
        external_target = cast(list[Mapping[str, object]], external_file["targets"])[0]
        assert external_target["position_basis"] == "lsp_zero_based_line_utf16_code_unit_character"
        assert "raw_range" in external_target and "range" not in external_target
        assert empty["files"] == [] and empty["omitted"] == 0

        bounded_errors = (
            await client.call_tool(
                "find_symbol",
                {
                    "relative_path": f"{prefix}python_symbols.py",
                    "name_path": "missing" * 200,
                    "max_answer_chars": 512,
                },
            ),
            await client.call_tool(
                "find_symbol",
                {
                    "relative_path": ("missing/" * 200) + "module.py",
                    "name_path": "missing",
                    "max_answer_chars": 512,
                },
            ),
            await client.call_tool(
                "find_declaration",
                {
                    "relative_path": f"{prefix}python_symbols.py",
                    "regex": r"(\w)" + "(?:)" * 300,
                    "max_answer_chars": 512,
                },
            ),
            await client.call_tool(
                "get_diagnostics_for_symbol",
                {
                    "relative_path": f"{prefix}python_symbols.py",
                    "name_path": "missing" * 200,
                    "max_answer_chars": 512,
                },
            ),
        )
        for result in bounded_errors:
            assert result.isError is not True
            assert len(result.content) == 1
            block = result.content[0]
            assert isinstance(block, types.TextContent)
            assert len(block.text) <= 512
            assert result.structuredContent is not None
            assert block.text == json.dumps(
                result.structuredContent,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            failure = cast(Mapping[str, object], result.structuredContent)
            assert failure["ok"] is False
            assert "adapter" not in failure and "generations" not in failure
        assert _mapping(_mapping(bounded_errors[0].structuredContent)["error"])["code"] == (
            "SYMBOL_NOT_FOUND"
        )
        assert _mapping(_mapping(bounded_errors[1].structuredContent)["error"])["code"] in {
            "INVALID_INPUT",
            "INVALID_PATH",
        }
        declaration_error = _mapping(_mapping(bounded_errors[2].structuredContent)["error"])
        assert declaration_error["code"] == "AMBIGUOUS_SYMBOL"
        occurrence_count = _mapping(declaration_error["details"])["occurrence_count"]
        assert isinstance(occurrence_count, int) and occurrence_count > 1
        diagnostic_error = _mapping(_mapping(bounded_errors[3].structuredContent)["error"])
        assert diagnostic_error["code"] == "SYMBOL_NOT_FOUND"
        diagnostic_details = _mapping(diagnostic_error["details"])
        assert "engine" not in diagnostic_details and "name_path" not in diagnostic_details

        invalid_budget_cases = (
            (
                "get_symbols_overview",
                {"relative_path": f"{prefix}python_symbols.py", "max_answer_chars": 10},
            ),
            (
                "find_symbol",
                {"name_path": "ANSWER", "max_answer_chars": 60_000},
            ),
            (
                "find_referencing_symbols",
                {
                    "relative_path": f"{prefix}python_symbols.py",
                    "name_path": "ANSWER",
                    "max_answer_chars": 100,
                },
            ),
            (
                "find_declaration",
                {
                    "relative_path": f"{prefix}python_usage.py",
                    "regex": r"import (ANSWER), Calculator",
                    "max_answer_chars": 100,
                },
            ),
            (
                "find_implementations",
                {
                    "relative_path": f"{prefix}typescript_symbols.ts",
                    "name_path": "Runner",
                    "max_answer_chars": 100,
                },
            ),
        )
        for tool, arguments in invalid_budget_cases:
            result = await client.call_tool(tool, arguments)
            assert result.isError is not True
            assert result.structuredContent is not None
            failure = cast(Mapping[str, object], result.structuredContent)
            assert failure["ok"] is False
            error_value = _mapping(failure["error"])
            assert error_value["code"] == "INVALID_INPUT"
            assert _mapping(error_value["details"])["field"] == "max_answer_chars"

        released = _data(await client.call_tool("release_workspace", {"immediate": True}))
        assert released["bound"] is False
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


def test_real_stdio_connector_returns_exact_compact_navigation_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """All five navigation tools cross the real connector with one exact compact representation."""

    runtime_root = (tmp_path / "isolated-compact-runtime").resolve()
    compact_workspace = (tmp_path / "compact-workspace").resolve()
    compact_workspace.mkdir()
    completed = subprocess.run(
        ["git", "init", "--quiet", str(compact_workspace)],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 0, completed.stderr
    fixture_source = REPOSITORY_ROOT / "tests/integration/fixtures/compact_navigation"
    for source in fixture_source.iterdir():
        if source.is_file():
            shutil.copy2(source, compact_workspace / source.name)
    typescript_symbols = compact_workspace / "typescript_symbols.ts"
    typescript_symbols.write_text(
        typescript_symbols.read_text() + "\nexport const ANSWER: number = 42;\n"
    )
    build_root: Path | None = None
    expected_daemon_id: str | None = None
    with _proxy_environment(monkeypatch, poisoned=False) as child_environment:
        child_environment.update(
            {
                cli.ACCEPTANCE_RUNTIME_ROOT_ENV: str(runtime_root),
                cli.ACCEPTANCE_BUILD_VARIANT_ENV: "compactSchema3RealConnector1",
                cli.ACCEPTANCE_WARM_GRACE_SECONDS_ENV: "1",
                cli.ACCEPTANCE_IDLE_EXIT_SECONDS_ENV: "8",
            }
        )
        acceptance = cli._acceptance_overrides(child_environment)
        assert acceptance is not None
        build_identity = cli._acceptance_build_identity(acceptance)
        build_root = prepare_runtime_layout(runtime_root, build_identity).build_root
        try:
            expected_daemon_id = asyncio.run(
                _run_compact_navigation_stdio_client(
                    compact_workspace,
                    child_environment=child_environment,
                    expected_build_identity=build_identity,
                    fixture_root="",
                )
            )
        finally:
            if build_root is not None and (metadata := _read_daemon(build_root)) is not None:
                owned = _DaemonProcess(metadata.daemon_id, metadata.pid, metadata.process_start_time)
                assert expected_daemon_id is None or owned.daemon_id == expected_daemon_id
                _terminate_owned_daemon(owned, _descendants(owned))


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
