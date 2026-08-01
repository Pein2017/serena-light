"""Check that a native tsserver program stays inside its Git trust boundary.

The Git JS/TS inventory is the trust boundary. A native project config may
legitimately select a smaller semantic program, but it may not admit ignored or
otherwise untrusted supported-language files.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import queue
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from serena_light.bootstrap import repository_root, runtime_paths

SUPPORTED_TYPESCRIPT = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
# Pinned real JS/TS acceptance root: a Git root whose own ``tsconfig.json``
# selects a strict subset of its Git-trusted JS/TS inventory.
EXTERNAL_TYPESCRIPT_ROOT = Path("/data/CoordExp/external/codexUI")
EXTERNAL_TYPESCRIPT_ENTRY = Path("src/api/codexErrors.ts")
SCHEMA_VERSION = 3
NATIVE_CONFIG_NAMES = {"tsconfig.json", "jsconfig.json"}
SCOPE_INCOMPATIBLE = "SCOPE_INCOMPATIBLE"


class TsServerClient:
    def __init__(self, command: list[str], cwd: Path) -> None:
        env = os.environ.copy()
        env["PATH"] = str(Path(command[0]).parent)
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self.sequence = 0
        self.cleanup_ok = False
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()

    def _read_loop(self) -> None:
        assert self.process.stdout is not None
        while True:
            line = self.process.stdout.readline()
            if not line:
                return
            if not line.lower().startswith(b"content-length:"):
                continue
            length = int(line.partition(b":")[2].strip())
            while self.process.stdout.readline() not in {b"\r\n", b"\n", b""}:
                pass
            message = json.loads(self.process.stdout.read(length))
            if message.get("type") == "response":
                self.responses.put(message)

    def command(self, name: str, arguments: dict[str, Any], *, wait: bool = True) -> dict[str, Any] | None:
        self.sequence += 1
        request = {"seq": self.sequence, "type": "request", "command": name, "arguments": arguments}
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        self.process.stdin.flush()
        if not wait:
            return None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            response = self.responses.get(timeout=max(0.1, deadline - time.monotonic()))
            if response.get("request_seq") == self.sequence:
                return response
        raise TimeoutError(f"tsserver did not answer {name}")

    def close(self) -> None:
        process_group = self.process.pid
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process_group, signal.SIGTERM)
        deadline = time.monotonic() + 2
        while self._group_alive(process_group) and time.monotonic() < deadline:
            time.sleep(0.05)
        if self._group_alive(process_group):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process_group, signal.SIGKILL)
        if self.process.poll() is None:
            self.process.wait(timeout=3)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()
        self.reader.join(timeout=1)
        self.cleanup_ok = not self._group_alive(process_group)

    @staticmethod
    def _group_alive(process_group: int) -> bool:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


def _path_digest(paths: list[str]) -> str:
    return hashlib.sha256("\0".join(paths).encode()).hexdigest()


def _normalize_paths(
    root: Path,
    raw_paths: list[str],
    *,
    source: str,
) -> tuple[set[str], list[dict[str, str]]]:
    """Return lexical in-root regular files and fail-closed path rejections."""
    lexical_root = root.resolve()
    accepted: set[str] = set()
    rejected: dict[tuple[str, str], dict[str, str]] = {}

    for raw in raw_paths:
        supplied = Path(raw)
        rooted = supplied if supplied.is_absolute() else lexical_root / supplied
        claims_root = not supplied.is_absolute() or rooted.is_relative_to(lexical_root)
        lexical = Path(os.path.normpath(rooted))
        try:
            relative = lexical.relative_to(lexical_root)
        except ValueError:
            if claims_root and supplied.suffix.lower() in SUPPORTED_TYPESCRIPT:
                path = supplied.as_posix()
                rejected[(path, "lexical_escape")] = {"path": path, "reason": "lexical_escape"}
            continue

        normalized = relative.as_posix()
        if normalized == "node_modules" or normalized.startswith("node_modules/"):
            continue
        if relative.suffix.lower() not in SUPPORTED_TYPESCRIPT:
            continue

        try:
            file_stat = lexical.lstat()
        except FileNotFoundError:
            reason = "tracked_deleted" if source == "git" else "missing"
            rejected[(normalized, reason)] = {"path": normalized, "reason": reason}
            continue

        try:
            resolved = lexical.resolve(strict=True)
        except (FileNotFoundError, RuntimeError):
            reason = "symlink" if stat.S_ISLNK(file_stat.st_mode) else "missing"
            rejected[(normalized, reason)] = {"path": normalized, "reason": reason}
            continue

        if stat.S_ISLNK(file_stat.st_mode) or resolved != lexical:
            reason = "symlink" if resolved.is_relative_to(lexical_root) else "symlink_escape"
            rejected[(normalized, reason)] = {"path": normalized, "reason": reason}
            continue
        if not stat.S_ISREG(file_stat.st_mode):
            rejected[(normalized, "non_regular")] = {"path": normalized, "reason": "non_regular"}
            continue
        accepted.add(normalized)

    return accepted, sorted(rejected.values(), key=lambda item: (item["path"], item["reason"]))


def _git_inventory(root: Path) -> tuple[set[str], list[dict[str, str]]]:
    raw = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    paths = [item.decode().replace("\\", "/") for item in raw.split(b"\0") if item]
    return _normalize_paths(root, paths, source="git")


def _selected_config(config_file_name: str | None, root: Path) -> tuple[str | None, str]:
    """Return a normalized native config path or identify an inferred project."""
    if not config_file_name:
        return None, "inferred"
    path = Path(config_file_name)
    if path.name not in NATIVE_CONFIG_NAMES or not path.is_file():
        return None, "inferred"
    resolved_root = root.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise RuntimeError(f"tsserver selected a config outside the root: {resolved}")
    return resolved.relative_to(resolved_root).as_posix(), "configured"


def _configured_program(root: Path, body: dict[str, Any]) -> tuple[set[str], list[dict[str, str]]]:
    return _normalize_paths(root, body.get("fileNames", []), source="configured_program")


def _path_set_evidence(paths: list[str]) -> dict[str, str | int]:
    return {"count": len(paths), "sha256": _path_digest(paths)}


def _probe_root(root: Path, entry: Path, locked: dict[str, Path], label: str) -> dict[str, Any]:
    inventory, inventory_rejected = _git_inventory(root)
    client = TsServerClient([str(locked["node"]), str(locked["tsserver"])], root)
    try:
        client.command("open", {"file": str(entry), "projectRootPath": str(root)}, wait=False)
        response = client.command("projectInfo", {"file": str(entry), "needFileNameList": True})
    finally:
        client.close()
    if not response or not response.get("success"):
        raise RuntimeError(f"projectInfo failed for {label}: {response}")
    body = response["body"]
    selected_config_path, project_kind = _selected_config(body.get("configFileName"), root)
    program, program_rejected = _configured_program(root, body)
    trusted_not_in_configured_program = sorted(inventory - program)
    configured_program_outside_trust = sorted(program - inventory)
    scope_compatible = not configured_program_outside_trust and not program_rejected
    incompatible_count = len(configured_program_outside_trust) + len(program_rejected)
    error = (
        None
        if scope_compatible
        else {
            "code": SCOPE_INCOMPATIBLE,
            "message": (
                f"{incompatible_count} configured program path(s) lie outside "
                "or cannot safely enter the Git trust boundary"
            ),
            "paths": configured_program_outside_trust,
            "rejected_paths": program_rejected,
        }
    )
    git_inventory = sorted(inventory)
    tsserver_program = sorted(program)
    difference_reasons = {
        "trusted_not_in_configured_program": [
            {"path": path, "reason": "excluded_by_native_config"} for path in trusted_not_in_configured_program
        ],
        "configured_program_outside_trust": [
            {"path": path, "reason": "absent_from_git_trust_inventory"} for path in configured_program_outside_trust
        ],
    }
    return {
        "label": label,
        "selected_config_path": selected_config_path,
        "project_kind": project_kind,
        "git_inventory": git_inventory,
        "tsserver_program": tsserver_program,
        "trusted_not_in_configured_program": trusted_not_in_configured_program,
        "configured_program_outside_trust": configured_program_outside_trust,
        "difference_reasons": difference_reasons,
        "path_set_evidence": {
            "git_inventory": _path_set_evidence(git_inventory),
            "tsserver_program": _path_set_evidence(tsserver_program),
            "trusted_not_in_configured_program": _path_set_evidence(trusted_not_in_configured_program),
            "configured_program_outside_trust": _path_set_evidence(configured_program_outside_trust),
        },
        "git_inventory_rejected": inventory_rejected,
        "configured_program_rejected": program_rejected,
        "rejected_path_counts": {
            "git_inventory": len(inventory_rejected),
            "configured_program": len(program_rejected),
        },
        "comparison_basis": "normalized_path_sets",
        "count_only_equivalence_rejected": True,
        "overlay_generated": False,
        "cleanup_ok": client.cleanup_ok,
        "scope_compatible": scope_compatible,
        "error": error,
        "status": "pass" if scope_compatible and client.cleanup_ok else "fail",
    }


def _probe_path_scoped_omission(
    root: Path,
    configured_entry: Path,
    candidate: str,
    locked: dict[str, Path],
) -> dict[str, Any]:
    candidate_path = root / candidate
    client = TsServerClient([str(locked["node"]), str(locked["tsserver"])], root)
    try:
        client.command("open", {"file": str(configured_entry), "projectRootPath": str(root)}, wait=False)
        before = client.command("projectInfo", {"file": str(configured_entry), "needFileNameList": True})
        client.command("open", {"file": str(candidate_path), "projectRootPath": str(root)}, wait=False)
        candidate_info = client.command("projectInfo", {"file": str(candidate_path), "needFileNameList": True})
        service = client.command("navtree", {"file": str(candidate_path)})
        after = client.command("projectInfo", {"file": str(configured_entry), "needFileNameList": True})
    finally:
        client.close()

    responses_ok = all(response and response.get("success") for response in (before, candidate_info, service, after))
    if not before or not candidate_info or not after:
        raise RuntimeError("tsserver omitted-file attribution returned no response")
    before_program, before_rejected = _configured_program(root, before["body"])
    after_program, after_rejected = _configured_program(root, after["body"])
    candidate_program, candidate_rejected = _configured_program(root, candidate_info["body"])
    selected_config_path, project_kind = _selected_config(candidate_info["body"].get("configFileName"), root)
    before_paths = sorted(before_program)
    after_paths = sorted(after_program)
    configured_program_unchanged = (
        before_paths == after_paths
        and before_rejected == after_rejected
        and before["body"].get("configFileName") == after["body"].get("configFileName")
    )
    service_supported = responses_ok and candidate in candidate_program and not candidate_rejected
    engine_owned = project_kind == "inferred" and selected_config_path is None
    passed = service_supported and engine_owned and configured_program_unchanged and client.cleanup_ok
    error = None
    if not passed:
        error = {
            "code": "PATH_SCOPED_UNSUPPORTED",
            "message": "tsserver did not preserve an engine-owned inferred path-scoped service boundary",
        }
    return {
        "path": candidate,
        "operation": "navtree",
        "service_supported": service_supported,
        "engine_owned": engine_owned,
        "project_kind": project_kind,
        "selected_config_path": selected_config_path,
        "read_only": True,
        "configured_program_unchanged": configured_program_unchanged,
        "global_scope_expanded": not configured_program_unchanged,
        "configured_program_before_sha256": _path_digest(before_paths),
        "configured_program_after_sha256": _path_digest(after_paths),
        "cleanup_ok": client.cleanup_ok,
        "status": "pass" if passed else "unsupported",
        "error": error,
    }


def run_probe() -> dict[str, Any]:
    repo = repository_root()
    fixture = repo / "tests" / "admission" / "fixtures" / "ts-scope"
    locked = runtime_paths(repo)
    with tempfile.TemporaryDirectory(prefix="serena-light-ts-scope-") as temp:
        root = Path(temp) / "fixture"
        shutil.copytree(fixture, root)
        outside = Path(temp) / "outside.ts"
        outside.write_text("export const outside = true;\n")
        (root / "tracked-deleted.ts").write_text("export const deleted = true;\n")
        (root / "tracked-inside-link.ts").symlink_to("src/helper.ts")
        (root / "tracked-outside-link.ts").symlink_to("../outside.ts")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        (root / "tracked-deleted.ts").unlink()
        fixture_result = _probe_root(root, root / "src" / "main.ts", locked, "ignored-subtree fixture")
    actual_result = _probe_root(
        EXTERNAL_TYPESCRIPT_ROOT,
        EXTERNAL_TYPESCRIPT_ROOT / EXTERNAL_TYPESCRIPT_ENTRY,
        locked,
        "codexUI actual root",
    )
    omitted_mjs = next(
        (path for path in actual_result["trusted_not_in_configured_program"] if Path(path).suffix.lower() == ".mjs"),
        None,
    )
    if omitted_mjs is None:
        raise RuntimeError(f"{EXTERNAL_TYPESCRIPT_ROOT} has no Git-trusted MJS path omitted by its native config")
    actual_result["path_scoped_omission_probe"] = _probe_path_scoped_omission(
        EXTERNAL_TYPESCRIPT_ROOT,
        EXTERNAL_TYPESCRIPT_ROOT / EXTERNAL_TYPESCRIPT_ENTRY,
        omitted_mjs,
        locked,
    )
    actual_result["cleanup_ok"] = (
        actual_result["cleanup_ok"] and actual_result["path_scoped_omission_probe"]["cleanup_ok"]
    )
    checks = {"ignored_subtree_fixture": fixture_result, "external_typescript_root": actual_result}
    fixture_detection_ok = (
        fixture_result["selected_config_path"] == "tsconfig.json"
        and fixture_result["project_kind"] == "configured"
        and fixture_result["configured_program_outside_trust"] == ["ignored-generated/hidden.ts"]
        and not fixture_result["scope_compatible"]
        and fixture_result["error"] is not None
        and fixture_result["error"]["code"] == SCOPE_INCOMPATIBLE
        and fixture_result["cleanup_ok"]
    )
    actual_scope_ok = (
        actual_result["selected_config_path"] == "tsconfig.json"
        and actual_result["project_kind"] == "configured"
        and actual_result["scope_compatible"]
        and actual_result["cleanup_ok"]
        and actual_result["path_scoped_omission_probe"]["status"] == "pass"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "fixture": "tests/admission/fixtures/ts-scope",
        "checks": checks,
        "overlay_generated": False,
        "status": "pass" if fixture_detection_ok and actual_scope_ok else "fail",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = run_probe()
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    print(rendered, end="")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
