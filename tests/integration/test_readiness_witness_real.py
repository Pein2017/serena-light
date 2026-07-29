"""Locked-engine witnesses for the task-4.8 readiness contract.

These probes exercise the real LSP processes, but deliberately model no
production readiness state.  They establish the facts an adapter must retain:
an empty ``publishDiagnostics`` is document evidence only, and a global-ready
claim needs a current-generation ``workspace/symbol`` witness.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import psutil
import pytest

from serena_light.bootstrap import repository_root, runtime_paths
from serena_light.lsp.client import SyncLspClient
from serena_light.processes import terminate_process_tree_with_kill_fallback

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.timeout(75)

_DOCUMENT_WAIT_SECONDS = 12.0
_GLOBAL_WAIT_SECONDS = 30.0


class WitnessState(StrEnum):
    CLEAN_PUBLICATION = "clean_publication"
    CURRENT_DOCUMENT_RESPONSE = "current_document_response"
    NOT_READY_NO_DOCUMENT_SYMBOL = "not_ready_no_document_symbol"


@dataclass(frozen=True)
class DocumentWitness:
    state: WitnessState
    uri: str
    publication_received: bool
    symbol_names: tuple[str, ...]


@dataclass(frozen=True)
class GlobalSentinel:
    generation: int
    name: str
    uri: str


@dataclass(frozen=True)
class EngineFixture:
    name: str
    language: str
    extension: str
    language_id: str

    @property
    def root(self) -> Path:
        return ROOT / "tests" / "integration" / "fixtures" / "readiness" / self.name

    @property
    def sentinel(self) -> Path:
        return self.root / "src" / f"sentinel{self.extension}"

    @property
    def empty(self) -> Path:
        return self.root / "src" / f"empty{self.extension}"


FIXTURES = (
    EngineFixture("python", "python", ".py", "python"),
    EngineFixture("typescript", "typescript", ".ts", "typescript"),
)


def _path_uri(path: Path) -> str:
    return "file://" + quote(str(path.resolve()))


class LockedEngineClient:
    """Small real-process wrapper around the owned synchronous LSP client."""

    def __init__(self, command: list[str], root: Path, settings: dict[str, Any]) -> None:
        self.command = command
        self.root = root
        self.settings = settings
        self.process: subprocess.Popen[bytes] | None = None
        self.client: SyncLspClient | None = None
        self.notifications: deque[dict[str, Any]] = deque(maxlen=100)
        self.cleanup_ok = False

    def start(self) -> None:
        env = os.environ.copy()
        env["PATH"] = str(Path(self.command[0]).parent)
        env.pop("NODE_PATH", None)
        self.process = subprocess.Popen(
            self.command,
            cwd=self.root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert self.process.stdin is not None and self.process.stdout is not None
        self.client = SyncLspClient(
            self.process.stdout,
            self.process.stdin,
            request_timeout=_GLOBAL_WAIT_SECONDS,
            notification_handler=self._notification,
            request_handlers={"workspace/configuration": self._configuration},
        )
        self.client.start()

    def _notification(self, method: str, params: Any) -> None:
        self.notifications.append({"method": method, "params": params})

    def _configuration(self, params: Any) -> list[dict[str, Any]]:
        items = params.get("items", []) if isinstance(params, dict) else []
        result: list[dict[str, Any]] = []
        for item in items:
            section = item.get("section") if isinstance(item, dict) else None
            if section == "python":
                result.append({"pythonPath": self.settings.get("pythonPath")})
            elif section == "python.analysis":
                result.append(self.settings.get("analysis", {}))
            elif section == "pyright":
                result.append(self.settings.get("pyright", {}))
            else:
                result.append({})
        return result

    def request(self, method: str, params: dict[str, Any] | None, timeout: float) -> Any:
        assert self.client is not None
        return self.client.request(method, params, timeout=timeout)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        assert self.client is not None
        self.client.notify(method, params)

    def close(self) -> None:
        process = self.process
        if process is None:
            self.cleanup_ok = True
            return
        try:
            root_process = psutil.Process(process.pid)
            owned_identities = [
                (owned.pid, owned.create_time()) for owned in [root_process, *root_process.children(recursive=True)]
            ]
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            owned_identities = [(process.pid, None)]
        try:
            process_group = os.getpgid(process.pid)
        except ProcessLookupError:
            process_group = None
        if self.client is not None:
            self.client.shutdown(timeout=3.0)
        if process.poll() is None:
            terminate_process_tree_with_kill_fallback(process, 2.0, "readiness witness", kill_timeout=2.0)
        still_live = any(_same_live_process(pid, create_time) for pid, create_time in owned_identities)
        if still_live and process_group is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process_group, signal.SIGTERM)
        deadline = time.monotonic() + 2.0
        while still_live and time.monotonic() < deadline:
            time.sleep(0.05)
            still_live = any(_same_live_process(pid, create_time) for pid, create_time in owned_identities)
        if still_live and process_group is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process_group, signal.SIGKILL)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        self.cleanup_ok = not any(_same_live_process(pid, create_time) for pid, create_time in owned_identities)


def _same_live_process(pid: int, create_time: float | None) -> bool:
    try:
        process = psutil.Process(pid)
        matches_identity = create_time is None or process.create_time() == create_time
        return matches_identity and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return False


def _command_and_settings(fixture: EngineFixture) -> tuple[list[str], dict[str, Any], Path | None]:
    locked = runtime_paths(repository_root())
    if fixture.language == "python":
        interpreter = Path("/root/miniconda3/envs/ms/bin/python")
        return (
            [str(locked["node"]), str(locked["pyright-langserver"]), "--stdio"],
            {
                "pythonPath": str(interpreter),
                "analysis": {
                    "diagnosticMode": "workspace",
                    "autoSearchPaths": True,
                    "useLibraryCodeForTypes": True,
                },
            },
            interpreter,
        )
    return [str(locked["node"]), str(locked["typescript-language-server"]), "--stdio"], {}, None


def _initialize_params(fixture: EngineFixture, interpreter: Path | None, tsserver: Path) -> dict[str, Any]:
    root_uri = _path_uri(fixture.root)
    options: dict[str, Any]
    if fixture.language == "python":
        assert interpreter is not None
        options = {"reportMissingImports": "none"}
    else:
        options = {
            "preferences": {"disableAutomaticTypingAcquisition": True},
            "tsserver": {"path": str(tsserver)},
        }
    return {
        "processId": os.getpid(),
        "clientInfo": {"name": "serena-light-readiness-witness", "version": "0.1.0"},
        "rootPath": str(fixture.root),
        "rootUri": root_uri,
        "workspaceFolders": [{"uri": root_uri, "name": fixture.root.name}],
        "capabilities": {
            "general": {"positionEncodings": ["utf-16", "utf-8", "utf-32"]},
            "workspace": {"workspaceFolders": True, "configuration": True, "symbol": {}},
            "textDocument": {"documentSymbol": {"hierarchicalDocumentSymbolSupport": True}},
        },
        "initializationOptions": options,
    }


def _open(client: LockedEngineClient, fixture: EngineFixture, path: Path, version: int, text: str) -> str:
    uri = _path_uri(path)
    client.notify(
        "textDocument/didOpen",
        {
            "textDocument": {
                "uri": uri,
                "languageId": fixture.language_id,
                "version": version,
                "text": text,
            }
        },
    )
    return uri


def _change(client: LockedEngineClient, uri: str, version: int, text: str) -> None:
    client.notify(
        "textDocument/didChange",
        {"textDocument": {"uri": uri, "version": version}, "contentChanges": [{"text": text}]},
    )


def _symbol_names(result: Any) -> tuple[str, ...]:
    """Collect names from either hierarchical or legacy document-symbol shapes."""
    names: list[str] = []

    def visit(item: object) -> None:
        if not isinstance(item, dict):
            return
        mapping = cast(dict[str, object], item)
        name = mapping.get("name")
        if isinstance(name, str):
            names.append(name)
        children = mapping.get("children")
        if isinstance(children, list):
            for child in children:
                visit(child)

    if isinstance(result, list):
        for symbol in result:
            visit(symbol)
    return tuple(sorted(set(names)))


def _document_symbols(client: LockedEngineClient, uri: str, timeout: float = _DOCUMENT_WAIT_SECONDS) -> tuple[str, ...]:
    result = client.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}}, timeout) or []
    return _symbol_names(result)


def _matching_clean_publication(client: LockedEngineClient, uri: str, start: int) -> bool:
    for message in list(client.notifications)[start:]:
        if message.get("method") != "textDocument/publishDiagnostics":
            continue
        params = message.get("params")
        if not isinstance(params, dict) or params.get("uri") != uri:
            continue
        diagnostics = params.get("diagnostics")
        assert diagnostics == [], f"clean readiness fixture published findings: {diagnostics!r}"
        return True
    return False


def _document_witness(client: LockedEngineClient, uri: str, notification_start: int) -> DocumentWitness:
    deadline = time.monotonic() + _DOCUMENT_WAIT_SECONDS
    names: tuple[str, ...] = ()
    publication_received = False
    while time.monotonic() < deadline:
        names = _document_symbols(client, uri, max(0.1, deadline - time.monotonic()))
        publication_received = _matching_clean_publication(client, uri, notification_start)
        if names and publication_received:
            return DocumentWitness(WitnessState.CLEAN_PUBLICATION, uri, True, names)
        time.sleep(0.05)
    if not names:
        # A clean publication without a current document-symbol response cannot
        # promote an empty/no-symbol document to global-ready.
        return DocumentWitness(WitnessState.NOT_READY_NO_DOCUMENT_SYMBOL, uri, publication_received, ())
    # Some servers do not reliably publish diagnostics for a clean open
    # document. The successful current-document response is the needed fallback
    # witness, not an inferred clean diagnostic state.
    return DocumentWitness(WitnessState.CURRENT_DOCUMENT_RESPONSE, uri, publication_received, names)


def _global_sentinel(client: LockedEngineClient, uri: str, generation: int) -> GlobalSentinel:
    """Derive the exact global query from a current configured-file response."""
    names = _document_symbols(client, uri)
    assert names, "configured fixture file yielded no document-symbol candidate"
    name = names[0]
    deadline = time.monotonic() + _GLOBAL_WAIT_SECONDS
    last_result: object = None
    while time.monotonic() < deadline:
        last_result = client.request("workspace/symbol", {"query": name}, max(0.1, deadline - time.monotonic()))
        for item in last_result or []:
            if not isinstance(item, dict) or item.get("name") != name:
                continue
            location = item.get("location")
            if isinstance(location, dict) and location.get("uri") == uri:
                return GlobalSentinel(generation=generation, name=name, uri=uri)
        time.sleep(0.1)
    raise AssertionError(f"workspace/symbol did not return exact current candidate {name!r} at {uri}: {last_result!r}")


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda item: item.name)
def test_locked_engine_readiness_witnesses(fixture: EngineFixture, record_property: Any) -> None:
    """Exercise clean didOpen, a derived global sentinel, and a no-symbol file."""
    command, settings, interpreter = _command_and_settings(fixture)
    locked = runtime_paths(repository_root())
    client = LockedEngineClient(command, fixture.root, settings)
    try:
        client.start()
        client.request(
            "initialize",
            _initialize_params(fixture, interpreter, locked["tsserver"]),
            15.0,
        )
        client.notify("initialized", {})
        client.notify("workspace/didChangeConfiguration", {"settings": settings})

        original = fixture.sentinel.read_text(encoding="utf-8")
        notification_start = len(client.notifications)
        uri = _open(client, fixture, fixture.sentinel, 1, original)
        document = _document_witness(client, uri, notification_start)
        assert document.state in {WitnessState.CLEAN_PUBLICATION, WitnessState.CURRENT_DOCUMENT_RESPONSE}
        assert document.symbol_names
        record_property("document_witness", document.state)

        first = _global_sentinel(client, uri, generation=1)
        changed = original.replace(first.name, f"{first.name}Generation2", 1)
        assert changed != original
        _change(client, uri, 2, changed)
        second = _global_sentinel(client, uri, generation=2)
        assert second.generation == 2
        assert second.name == f"{first.name}Generation2"
        assert second.uri == first.uri

        empty_start = len(client.notifications)
        empty_uri = _open(client, fixture, fixture.empty, 1, fixture.empty.read_text(encoding="utf-8"))
        empty = _document_witness(client, empty_uri, empty_start)
        assert empty.state is WitnessState.NOT_READY_NO_DOCUMENT_SYMBOL
        assert empty.uri == empty_uri
        assert empty.symbol_names == ()
        record_property("empty_document_witness", empty.state)
    finally:
        client.close()

    assert client.cleanup_ok is True
