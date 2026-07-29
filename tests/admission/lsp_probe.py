"""Run clean-start global-readiness probes against the locked language engines.

This probe deliberately implements only enough JSON-RPC/LSP to test the
dependency and readiness assumptions before the product protocol core exists.
It must not be imported by production code.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from serena_light.bootstrap import EXPECTED_VERSIONS, inspect_runtime, lock_digest, repository_root, runtime_paths
from tests.admission.pyright_scope_probe import (
    bounded_trust_inventory,
    git_trust_inventory,
    probe_pyright_scope,
)

SUPPORTED_PYTHON = {".py", ".pyi"}
SUPPORTED_TYPESCRIPT = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True)
class Profile:
    name: str
    language: str
    root: Path
    symbol: str
    expected_relative_path: Path
    interpreter: Path | None = None
    representative: Path | None = None


@dataclass
class RunResult:
    profile: str
    run: int
    root: str
    symbol: str
    language: str
    initialize_seconds: float
    global_ready_seconds: float
    query_seconds: float
    result_count: int
    matched_names: list[str]
    matched_uri: str
    inventory_count: int
    inventory_digest: str
    inventory_stable: bool
    server_source_count: int | None
    effective_scope_ok: bool | None
    scope_attribution: dict[str, Any] | None
    cleanup_ok: bool
    position_encoding: str
    position_encoding_source: str
    status: str
    error: str | None = None


def path_uri(path: Path) -> str:
    return "file://" + quote(str(path.resolve()))


def _transformers_root() -> Path:
    command = [
        "/root/miniconda3/envs/ms/bin/python",
        "-c",
        "import transformers; print(transformers.__path__[0])",
    ]
    return Path(subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip())


def profiles() -> dict[str, Profile]:
    ms_python = Path("/root/miniconda3/envs/ms/bin/python")
    return {
        "transformers": Profile(
            "transformers",
            "python",
            _transformers_root(),
            "Qwen2VLForConditionalGeneration",
            Path("models/qwen2_vl/modeling_qwen2_vl.py"),
            ms_python,
        ),
        "coordexp": Profile(
            "coordexp",
            "python",
            Path("/data/CoordExp"),
            "PipelinePlanner",
            Path("public_data/pipeline/planner.py"),
            ms_python,
        ),
        "ms-swift": Profile(
            "ms-swift",
            "python",
            Path("/data/ms-swift"),
            "SwiftPipeline",
            Path("swift/pipelines/base.py"),
            ms_python,
        ),
        "cc-plugin-codex": Profile(
            "cc-plugin-codex",
            "typescript",
            Path("/data/CoordExp/cc-plugin-codex"),
            "createAgentStore",
            Path("runtime/agent-store.mjs"),
            representative=Path("runtime/args.mjs"),
        ),
    }


def git_inventory(root: Path, extensions: set[str]) -> list[str]:
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    inventory = []
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8", "surrogateescape")
        path = root / relative
        if path.suffix.lower() in extensions and path.is_file() and not path.is_symlink():
            inventory.append(relative)
    return sorted(inventory)


def inventory_digest(inventory: list[str]) -> str:
    return hashlib.sha256("\0".join(inventory).encode("utf-8", "surrogateescape")).hexdigest()


def bounded_inventory(root: Path, extensions: set[str]) -> list[str]:
    inventory: list[str] = []
    excluded_names = {".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", "node_modules"}
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = [name for name in names if name not in excluded_names and not name.startswith(".")]
        base = Path(directory)
        for name in files:
            path = base / name
            if path.suffix.lower() in extensions and not path.is_symlink():
                inventory.append(path.relative_to(root).as_posix())
    return sorted(inventory)


def server_source_count(notifications: deque[dict[str, Any]]) -> int | None:
    for message in reversed(notifications):
        text = str((message.get("params") or {}).get("message", ""))
        match = re.search(r"Found (\d+) source files?", text)
        if match:
            return int(match.group(1))
    return None


class LspClient:
    def __init__(self, command: list[str], root: Path, settings: dict[str, Any]) -> None:
        self.command = command
        self.root = root
        self.settings = settings
        self.process: subprocess.Popen[bytes] | None = None
        self._next_id = 1
        self._responses: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self.notifications: deque[dict[str, Any]] = deque(maxlen=100)
        self.stderr: deque[str] = deque(maxlen=100)
        self.initialize_exchange: dict[str, Any] = {}
        self.cleanup_ok = False
        self.source_count: int | None = None

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
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._stderr_reader = threading.Thread(target=self._stderr_loop, daemon=True)
        self._reader.start()
        self._stderr_reader.start()

    def _stderr_loop(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        for line in self.process.stderr:
            self.stderr.append(line.decode("utf-8", "replace").rstrip())

    def _read_message(self) -> dict[str, Any] | None:
        assert self.process is not None and self.process.stdout is not None
        content_length: int | None = None
        while True:
            line = self.process.stdout.readline()
            if not line:
                return None
            if line in {b"\r\n", b"\n"}:
                break
            key, _, value = line.partition(b":")
            if key.lower() == b"content-length":
                content_length = int(value.strip())
        if content_length is None:
            return None
        body = self.process.stdout.read(content_length)
        return json.loads(body)

    def _read_loop(self) -> None:
        while True:
            try:
                message = self._read_message()
            except (json.JSONDecodeError, OSError, ValueError) as exc:
                self.stderr.append(f"probe reader error: {exc}")
                return
            if message is None:
                return
            if "method" in message and "id" in message:
                self._answer_server_request(message)
            elif "id" in message:
                with self._lock:
                    waiter = self._responses.get(int(message["id"]))
                if waiter is not None:
                    waiter.put(message)
            else:
                text = str((message.get("params") or {}).get("message", ""))
                source_match = re.search(r"Found (\d+) source files?", text)
                if source_match:
                    self.source_count = int(source_match.group(1))
                self.notifications.append(message)

    def _answer_server_request(self, message: dict[str, Any]) -> None:
        method = message["method"]
        params = message.get("params") or {}
        if method == "workspace/configuration":
            result = []
            for item in params.get("items", []):
                section = item.get("section") if isinstance(item, dict) else None
                if section == "python":
                    result.append({"pythonPath": self.settings.get("pythonPath")})
                elif section == "python.analysis":
                    result.append(self.settings.get("analysis", {}))
                elif section == "pyright":
                    result.append(self.settings.get("pyright", {}))
                else:
                    result.append({})
        elif method == "workspace/executeClientCommand":
            result = []
        elif method == "workspace/applyEdit":
            result = {"applied": False, "failureReason": "admission probe is read-only"}
        else:
            result = None
        self._write({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def _write(self, message: dict[str, Any]) -> None:
        assert self.process is not None and self.process.stdin is not None
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
        with self._lock:
            self.process.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            self.process.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None, timeout: float) -> Any:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            self._responses[request_id] = waiter
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        try:
            response = waiter.get(timeout=timeout)
        except queue.Empty as exc:
            process_status = None if self.process is None else self.process.poll()
            raise TimeoutError(
                f"timed out waiting for {method}; process={process_status}; stderr={list(self.stderr)[-10:]}"
            ) from exc
        finally:
            with self._lock:
                self._responses.pop(request_id, None)
        if "error" in response:
            raise RuntimeError(f"{method} failed: {response['error']}")
        return response.get("result")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def close(self) -> None:
        process = self.process
        if process is None:
            self.cleanup_ok = True
            return
        process_group = process.pid
        if process.poll() is None:
            try:
                self.request("shutdown", None, 3.0)
                self.notify("exit")
                process.wait(timeout=3.0)
            except (TimeoutError, RuntimeError, subprocess.TimeoutExpired, BrokenPipeError):
                pass
        deadline = time.monotonic() + 2.0
        while self._process_group_alive(process_group) and time.monotonic() < deadline:
            time.sleep(0.05)
        if self._process_group_alive(process_group):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process_group, signal.SIGTERM)
            deadline = time.monotonic() + 2.0
            while self._process_group_alive(process_group) and time.monotonic() < deadline:
                time.sleep(0.05)
        if self._process_group_alive(process_group):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process_group, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2.0)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        for thread in (self._reader, self._stderr_reader):
            if thread is not None:
                thread.join(timeout=1.0)
        self.cleanup_ok = not self._process_group_alive(process_group)

    @staticmethod
    def _process_group_alive(process_group: int) -> bool:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


def initialize_params(profile: Profile, tsserver_path: Path | None = None) -> dict[str, Any]:
    root_uri = path_uri(profile.root)
    capabilities = {
        "general": {"positionEncodings": ["utf-16", "utf-8", "utf-32"]},
        "workspace": {
            "workspaceFolders": True,
            "configuration": True,
            "didChangeConfiguration": {"dynamicRegistration": True},
            "symbol": {"dynamicRegistration": True},
        },
        "window": {"workDoneProgress": True},
        "textDocument": {
            "synchronization": {"dynamicRegistration": True, "didSave": True},
            "definition": {"dynamicRegistration": True},
            "references": {"dynamicRegistration": True},
            "documentSymbol": {
                "dynamicRegistration": True,
                "hierarchicalDocumentSymbolSupport": True,
            },
            "publishDiagnostics": {"relatedInformation": True},
        },
    }
    options: dict[str, Any]
    if profile.language == "python":
        # Preserve the repository's native Pyright configuration. Trust is
        # compared with the engine program after attribution; it is not injected
        # here as an exclusion overlay.
        options = {"reportMissingImports": "none"}
    else:
        if tsserver_path is None:
            raise ValueError("TypeScript initialization requires the locked tsserver path")
        options = {
            "preferences": {"disableAutomaticTypingAcquisition": True},
            "tsserver": {"path": str(tsserver_path)},
        }
    return {
        "processId": os.getpid(),
        "clientInfo": {"name": "serena-light-admission", "version": "0.1.0"},
        "rootPath": str(profile.root),
        "rootUri": root_uri,
        "workspaceFolders": [{"uri": root_uri, "name": profile.root.name}],
        "capabilities": capabilities,
        "initializationOptions": options,
        "trace": "off",
    }


def _normalized_initialize_fixture(
    profile: Profile,
    request: dict[str, Any],
    response: Any,
    locked: dict[str, Path],
) -> dict[str, Any]:
    normalized_request = json.loads(json.dumps(request))
    normalized_request["processId"] = 0
    normalized_request["rootPath"] = "/WORKSPACE"
    normalized_request["rootUri"] = "file:///WORKSPACE"
    normalized_request["workspaceFolders"] = [{"uri": "file:///WORKSPACE", "name": "WORKSPACE"}]
    options = normalized_request.get("initializationOptions", {})
    original_exclude_count = len(options.get("exclude", []))
    if "exclude" in options:
        options["exclude"] = sorted(options["exclude"])[:40]
    capabilities = response.get("capabilities", {}) if isinstance(response, dict) else {}
    selected_encoding = capabilities.get("positionEncoding", "utf-16")
    engine_key = "pyright-langserver" if profile.language == "python" else "typescript-language-server"
    version_key = "pyright" if profile.language == "python" else "typescript-language-server"
    return {
        "schema_version": 1,
        "engine": profile.language,
        "engine_path": str(locked[engine_key]),
        "engine_version": EXPECTED_VERSIONS[version_key],
        "lock_digest": lock_digest(repository_root()),
        "selected_position_encoding": selected_encoding,
        "position_encoding_source": (
            "initialize.capabilities.positionEncoding"
            if "positionEncoding" in capabilities
            else "LSP default (server omitted positionEncoding)"
        ),
        "truncation": {
            "initialize_options_exclude_original_count": original_exclude_count,
            "initialize_options_exclude_retained_count": min(original_exclude_count, 40),
        },
        "request": normalized_request,
        "response": response,
    }


def run_once(
    profile: Profile,
    run_number: int,
    timeout: float,
    write_transcript: bool,
    *,
    scope_attribution: dict[str, Any] | None = None,
) -> RunResult:
    root = repository_root()
    locked = runtime_paths(root)
    extensions = SUPPORTED_PYTHON if profile.language == "python" else SUPPORTED_TYPESCRIPT
    is_git = (profile.root / ".git").exists()
    inventory = git_inventory(profile.root, extensions) if is_git else bounded_inventory(profile.root, extensions)
    settings: dict[str, Any] = {}
    if profile.language == "python":
        settings = {
            "pythonPath": str(profile.interpreter),
            "analysis": {
                "diagnosticMode": "workspace",
                "autoSearchPaths": True,
                "useLibraryCodeForTypes": True,
            },
        }
        command = [str(locked["node"]), str(locked["pyright-langserver"]), "--stdio"]
    else:
        command = [
            str(locked["node"]),
            str(locked["typescript-language-server"]),
            "--stdio",
        ]

    client = LspClient(command, profile.root, settings)
    started = time.monotonic()
    try:
        if profile.language == "python":
            assert profile.interpreter is not None
            if scope_attribution is None:
                scope_attribution = probe_pyright_scope(
                    profile.root,
                    profile.interpreter,
                    locked["node"],
                    locked["pyright"],
                    timeout=max(90.0, timeout),
                )
            inventory = list(scope_attribution["trust_inventory_paths"])
            if not scope_attribution["scope_compatible"]:
                raise RuntimeError(f"Pyright configured program is scope-incompatible: {scope_attribution['error']}")
        # Scope attribution is a separate admission phase; the 30-second
        # readiness clock begins at the clean language-server start.
        started = time.monotonic()
        client.start()
        params = initialize_params(profile, locked["tsserver"] if profile.language == "typescript" else None)
        response = client.request("initialize", params, min(timeout, 15.0))
        initialized_at = time.monotonic()
        client.notify("initialized", {})
        client.notify("workspace/didChangeConfiguration", {"settings": settings})
        if profile.representative is not None:
            representative = profile.root / profile.representative
            client.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": path_uri(representative),
                        "languageId": "javascript",
                        "version": 1,
                        "text": representative.read_text(encoding="utf-8"),
                    }
                },
            )
        if write_transcript:
            fixture = _normalized_initialize_fixture(profile, params, response, locked)
            fixture_dir = root / "tests" / "admission" / "fixtures" / "initialize"
            fixture_dir.mkdir(parents=True, exist_ok=True)
            (fixture_dir / f"{profile.language}.json").write_text(
                json.dumps(fixture, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("initialize exhausted the readiness deadline")
        query_started = time.monotonic()
        result: list[dict[str, Any]] = []
        matched: list[str] = []
        matched_uri = ""
        expected_uri = path_uri(profile.root / profile.expected_relative_path)
        deadline = started + timeout
        while time.monotonic() < deadline and not matched:
            remaining = deadline - time.monotonic()
            result = client.request("workspace/symbol", {"query": profile.symbol}, remaining) or []
            matching_items = []
            for item in result:
                location = item.get("location") if isinstance(item, dict) else None
                uri = location.get("uri") if isinstance(location, dict) else None
                if item.get("name") == profile.symbol and uri == expected_uri:
                    matching_items.append(item)
            matched = sorted({str(item["name"]) for item in matching_items})
            if matching_items:
                matched_uri = expected_uri
            if not matched:
                time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        ready_at = time.monotonic()
        if not matched:
            raise RuntimeError(
                f"exact acceptance symbol {profile.symbol!r} at {expected_uri} absent from {len(result)} results"
            )
        if profile.language == "python":
            current_inventory = git_trust_inventory(profile.root) if is_git else bounded_trust_inventory(profile.root)
        else:
            current_inventory = git_inventory(profile.root, extensions)
        stable_inventory = inventory_digest(inventory) == inventory_digest(current_inventory)
        if not stable_inventory:
            raise RuntimeError("source inventory changed during the readiness probe")
        capabilities = response.get("capabilities", {}) if isinstance(response, dict) else {}
        encoding = capabilities.get("positionEncoding")
        source = "initialize.capabilities.positionEncoding"
        if not encoding:
            encoding = "utf-16"
            source = "LSP default (server omitted positionEncoding)"
        observed_source_count = client.source_count
        effective_scope: bool | None = None
        if scope_attribution is not None:
            attributed_source_count = scope_attribution["configured_source_count"]
            if observed_source_count is None or observed_source_count != attributed_source_count:
                raise RuntimeError(
                    "Pyright CLI/LSP configured source counts diverged: "
                    f"cli={attributed_source_count}, lsp={observed_source_count}"
                )
            effective_scope = bool(scope_attribution["scope_compatible"])
        client.close()
        if not client.cleanup_ok:
            raise RuntimeError("language-server process group survived probe cleanup")
        return RunResult(
            profile=profile.name,
            run=run_number,
            root=str(profile.root),
            symbol=profile.symbol,
            language=profile.language,
            initialize_seconds=round(initialized_at - started, 3),
            global_ready_seconds=round(ready_at - started, 3),
            query_seconds=round(ready_at - query_started, 3),
            result_count=len(result),
            matched_names=matched[:10],
            matched_uri=matched_uri,
            inventory_count=len(inventory),
            inventory_digest=inventory_digest(inventory),
            inventory_stable=stable_inventory,
            server_source_count=observed_source_count,
            effective_scope_ok=effective_scope,
            scope_attribution=scope_attribution,
            cleanup_ok=client.cleanup_ok,
            position_encoding=str(encoding),
            position_encoding_source=source,
            status="pass",
        )
    except Exception as exc:
        elapsed = time.monotonic() - started
        client.close()
        return RunResult(
            profile=profile.name,
            run=run_number,
            root=str(profile.root),
            symbol=profile.symbol,
            language=profile.language,
            initialize_seconds=0.0,
            global_ready_seconds=round(elapsed, 3),
            query_seconds=0.0,
            result_count=0,
            matched_names=[],
            matched_uri="",
            inventory_count=len(inventory),
            inventory_digest=inventory_digest(inventory),
            inventory_stable=False,
            server_source_count=client.source_count,
            effective_scope_ok=False,
            scope_attribution=scope_attribution,
            cleanup_ok=client.cleanup_ok,
            position_encoding="unknown",
            position_encoding_source="probe failed",
            status="fail",
            error=(
                f"{type(exc).__name__}: {exc}; "
                f"notifications={list(client.notifications)[-20:]}; stderr={list(client.stderr)[-10:]}"
            ),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", action="append", choices=sorted(profiles()), dest="selected")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument(
        "--scope-timeout",
        type=float,
        default=300.0,
        help="one-time per-Python-profile native program attribution timeout",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--write-transcripts", action="store_true")
    args = parser.parse_args(argv)
    inspect_runtime(repository_root())
    locked = runtime_paths(repository_root())
    selected = args.selected or list(profiles())
    results = []
    for profile_name in selected:
        profile = profiles()[profile_name]
        scope_attribution: dict[str, Any] | None = None
        if profile.language == "python":
            assert profile.interpreter is not None
            scope_attribution = probe_pyright_scope(
                profile.root,
                profile.interpreter,
                locked["node"],
                locked["pyright"],
                timeout=args.scope_timeout,
            )
        for run_number in range(1, args.runs + 1):
            result = run_once(
                profile,
                run_number,
                args.timeout,
                args.write_transcripts and run_number == 1,
                scope_attribution=scope_attribution,
            )
            results.append(asdict(result))
            print(json.dumps(results[-1], sort_keys=True), flush=True)
    report = {
        "schema_version": 1,
        "timeout_seconds": args.timeout,
        "results": results,
        "status": "pass" if all(item["status"] == "pass" for item in results) else "fail",
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
