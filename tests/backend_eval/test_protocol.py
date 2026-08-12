"""Interface, shared-runner, and source-ownership tests for the Phase 2 protocol plane.

``run_protocol_probe`` is exercised against a real subprocess that is a small fake LSP
server script, never a candidate backend (Pyright/ty/Pyrefly) -- proving the shared
transport/deadline/cleanup discipline without launching anything this task is not allowed
to launch.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts.backend_eval import protocol as protocol_module
from scripts.backend_eval.models import EnvironmentIdentity, ServiceConfigIdentity
from scripts.backend_eval.process import Deadline, DeadlineExceeded, monotonic_clock
from scripts.backend_eval.protocol import BackendProtocolSpec, run_protocol_probe
from scripts.backend_eval.runtime import SERVICE_CONFIG_RELPATHS, CandidateRuntime
from serena_light.lsp.adapter import AdapterRuntime, EngineMetadata, SubprocessAdapterRuntimeProvider
from serena_light.lsp.client import SyncLspClient
from serena_light.lsp.positions import PositionEncoding

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INTERPRETER_VERSION = "3.12.11"

_FAKE_SERVER_SCRIPT = r"""
import json
import sys


def _read(stream):
    length = None
    while True:
        line = stream.readline()
        if not line:
            raise SystemExit(0)
        if line in (b"\r\n", b"\n"):
            break
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    body = stream.read(length)
    return json.loads(body)


def _write(stream, payload):
    body = json.dumps(payload).encode()
    stream.write(("Content-Length: %d\r\n\r\n" % len(body)).encode())
    stream.write(body)
    stream.flush()


stdin = sys.stdin.buffer
stdout = sys.stdout.buffer
while True:
    message = _read(stdin)
    method = message.get("method")
    if method == "initialize":
        _write(
            stdout,
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"capabilities": {"definitionProvider": True, "referencesProvider": True}},
            },
        )
    elif method == "initialized":
        continue
    elif method == "shutdown":
        _write(stdout, {"jsonrpc": "2.0", "id": message["id"], "result": None})
    elif method == "exit":
        break
    elif "id" in message:
        _write(stdout, {"jsonrpc": "2.0", "id": message["id"], "result": {"echoed": method}})
"""


def _fake_runtime(tmp_path: Path) -> CandidateRuntime:
    digest = "1" * 64
    root = tmp_path / digest
    venv_bin = root / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    python_path = venv_bin / "python"
    python_path.symlink_to(sys.executable)
    (venv_bin / "ty").write_bytes(b"")
    (venv_bin / "pyrefly").write_bytes(b"")
    home, cache, config = root / "home", root / "cache", root / "config"
    for directory in (home, cache, config):
        directory.mkdir()
    manifest_path = root / "runtime-manifest.json"
    manifest_path.write_text("{}")
    return CandidateRuntime(
        root=root,
        python=python_path,
        ty=venv_bin / "ty",
        pyrefly=venv_bin / "pyrefly",
        lock_digest=digest,
        executable_hashes=(("pyrefly", "2" * 64), ("ty", "3" * 64)),
        home=home,
        cache=cache,
        config=config,
        manifest_path=manifest_path,
        manifest_sha256="4" * 64,
        environments=(
            EnvironmentIdentity(
                name="llm-framework-study",
                interpreter_path=str(python_path),
                interpreter_realpath=str(python_path),
                version=_INTERPRETER_VERSION,
            ),
            EnvironmentIdentity(
                name="ms",
                interpreter_path=str(python_path),
                interpreter_realpath=str(python_path),
                version=_INTERPRETER_VERSION,
            ),
        ),
        service_configs=tuple(
            ServiceConfigIdentity(
                backend=backend,
                config_path=str(config / relpath),
                config_sha256="5" * 64,
                home_path=str(home),
                cache_path=str(cache),
            )
            for backend, relpath in sorted(SERVICE_CONFIG_RELPATHS.items())
        ),
    )


def _fake_spec() -> BackendProtocolSpec:
    return BackendProtocolSpec(
        name="fake",
        build_command=lambda runtime: (str(runtime.python), "-c", _FAKE_SERVER_SCRIPT),
        initialize_params=lambda root: {"rootUri": root.as_uri(), "capabilities": {}},
        request_handlers=None,
        engine=lambda runtime: EngineMetadata(name="fake", version="0.0.0", executable=runtime.python),
        position_encoding=PositionEncoding.UTF16,
        diagnostics_mode="push",
    )


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, delta: float) -> None:
        self._now += delta


# --- BackendProtocolSpec --------------------------------------------------------


def test_build_command_receives_the_prepared_runtime(tmp_path: Path) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = BackendProtocolSpec(
        name="pyright",
        build_command=lambda runtime: (str(runtime.python), "--version"),
        initialize_params=lambda root: {"rootUri": root.as_uri()},
        request_handlers=None,
        engine=lambda runtime: EngineMetadata(name="pyright", version="1.1.403", executable=runtime.python),
        position_encoding=PositionEncoding.UTF16,
        diagnostics_mode="push",
    )

    assert spec.build_command(runtime) == (str(runtime.python), "--version")


def test_backend_protocol_spec_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="name"):
        BackendProtocolSpec(
            name="",
            build_command=lambda runtime: (str(runtime.python),),
            initialize_params=lambda root: {},
            request_handlers=None,
            engine=lambda runtime: EngineMetadata(name="x", version="0", executable=runtime.python),
            position_encoding=PositionEncoding.UTF16,
            diagnostics_mode="push",
        )


def test_backend_protocol_spec_rejects_unknown_diagnostics_mode() -> None:
    with pytest.raises(ValueError, match="diagnostics_mode"):
        BackendProtocolSpec(
            name="pyright",
            build_command=lambda runtime: (str(runtime.python),),
            initialize_params=lambda root: {},
            request_handlers=None,
            engine=lambda runtime: EngineMetadata(name="x", version="0", executable=runtime.python),
            position_encoding=PositionEncoding.UTF16,
            diagnostics_mode="poll",
        )


# --- run_protocol_probe: real fake-process lifecycle ----------------------------


def test_run_protocol_probe_initializes_and_runs_session_then_stops(tmp_path: Path) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec()
    deadline = Deadline.start(monotonic_clock, 30.0)

    def session(client: SyncLspClient) -> str:
        result = client.request("textDocument/hover", {})
        assert isinstance(result, dict)
        return str(result["echoed"])

    session_result = run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    assert session_result.result == "textDocument/hover"
    assert session_result.raw_providers.definition is True
    assert session_result.raw_providers.references is True
    assert session_result.raw_providers.implementation is False
    assert session_result.engine.name == "fake"


def test_run_protocol_probe_calls_shutdown_and_stop_even_when_session_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec()
    deadline = Deadline.start(monotonic_clock, 30.0)
    stop_calls: list[AdapterRuntime] = []
    original_stop = protocol_module.SubprocessAdapterRuntimeProvider.stop

    def spy_stop(self: SubprocessAdapterRuntimeProvider, adapter_runtime: AdapterRuntime) -> None:
        stop_calls.append(adapter_runtime)
        original_stop(self, adapter_runtime)

    monkeypatch.setattr(protocol_module.SubprocessAdapterRuntimeProvider, "stop", spy_stop)

    def session(client: SyncLspClient) -> None:
        del client
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    assert len(stop_calls) == 1


def test_run_protocol_probe_never_launches_when_deadline_already_expired(tmp_path: Path) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec()
    clock = _FakeClock()
    deadline = Deadline.start(clock, 1.0)
    clock.advance(2.0)

    def session(client: SyncLspClient) -> None:
        del client
        raise AssertionError("session must never run when the deadline is already expired")

    with pytest.raises(DeadlineExceeded):
        run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)


def test_run_protocol_probe_raises_deadline_exceeded_when_session_overruns(tmp_path: Path) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec()
    clock = _FakeClock()
    deadline = Deadline.start(clock, 5.0)

    def session(client: SyncLspClient) -> str:
        result = client.request("textDocument/hover", {})
        assert isinstance(result, dict)
        clock.advance(10.0)
        return str(result["echoed"])

    with pytest.raises(DeadlineExceeded):
        run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)


def test_run_protocol_probe_never_leaves_the_candidate_process_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _fake_runtime(tmp_path)
    spec = _fake_spec()
    deadline = Deadline.start(monotonic_clock, 30.0)
    processes: list[subprocess.Popen[bytes] | None] = []

    original_start = protocol_module.SubprocessAdapterRuntimeProvider.start

    def spy_start(
        self: SubprocessAdapterRuntimeProvider,
        *,
        notification_handler: Callable[[str, Any], None],
        terminal_handler: Callable[[BaseException], None],
    ) -> AdapterRuntime:
        adapter_runtime = original_start(
            self, notification_handler=notification_handler, terminal_handler=terminal_handler
        )
        processes.append(adapter_runtime.process)
        return adapter_runtime

    monkeypatch.setattr(protocol_module.SubprocessAdapterRuntimeProvider, "start", spy_start)

    def session(client: SyncLspClient) -> None:
        client.request("textDocument/hover", {})

    run_protocol_probe(spec, runtime, tmp_path, deadline=deadline, session=session)

    assert len(processes) == 1
    process = processes[0]
    assert process is not None
    assert process.wait(timeout=5) is not None


# --- Source ownership -----------------------------------------------------------


def _imported_module_names(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_protocol_module_never_imports_workspace_runtime_or_language_adapter() -> None:
    source = (_REPO_ROOT / "scripts" / "backend_eval" / "protocol.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="protocol.py")

    for module in _imported_module_names(tree):
        assert not module.startswith("serena_light.workspace"), module

    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)
    assert "LanguageAdapter" not in imported_names
    assert "WorkspaceRuntime" not in imported_names


def test_src_serena_light_never_imports_backend_eval_evaluation_code() -> None:
    root = _REPO_ROOT / "src" / "serena_light"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imported_module_names(tree):
            if module == "scripts" or module.startswith("scripts."):
                offenders.append(f"{path}: {module}")
    assert offenders == []
