"""Attribute Pyright's native workspace program without reimplementing its config.

The version-and-bundle-locked Node probe captures Pyright 1.1.403's native
``AnalyzerService.getOwnedFiles()`` immediately after its CLI applies options.
This module compares that engine-owned path set with an independent trust
inventory. It deliberately does not read or interpret pyrightconfig.json.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from serena_light.bootstrap import EXPECTED_VERSIONS

SUPPORTED_PYTHON = {".py", ".pyi"}
SCOPE_INCOMPATIBLE = "SCOPE_INCOMPATIBLE"
_EXCLUDED_DIRECTORY_NAMES = {
    ".git",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
}


def path_digest(paths: list[str]) -> str:
    return hashlib.sha256("\0".join(paths).encode("utf-8", "surrogateescape")).hexdigest()


def _git_toplevel(root: Path) -> Path | None:
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None
    return Path(completed.stdout.strip()).resolve()


def git_trust_inventory(root: Path) -> list[str]:
    """Return existing regular, non-symlink Python files visible to Git."""
    root = root.resolve()
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    inventory: list[str] = []
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8", "surrogateescape").replace("\\", "/")
        if Path(relative).suffix.lower() not in SUPPORTED_PYTHON:
            continue
        candidate = root / relative
        try:
            mode = candidate.lstat().st_mode
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError, RuntimeError):
            continue
        if not stat.S_ISREG(mode) or not resolved.is_relative_to(root):
            continue
        inventory.append(relative)
    return sorted(set(inventory))


def bounded_trust_inventory(root: Path) -> list[str]:
    """Walk one non-Git root without following symlinks or hidden/cache trees."""
    root = root.resolve()
    inventory: list[str] = []
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = sorted(
            name
            for name in names
            if name not in _EXCLUDED_DIRECTORY_NAMES
            and not name.startswith(".")
            and not (Path(directory) / name).is_symlink()
        )
        base = Path(directory)
        for name in sorted(files):
            candidate = base / name
            if candidate.suffix.lower() not in SUPPORTED_PYTHON:
                continue
            try:
                mode = candidate.lstat().st_mode
                resolved = candidate.resolve(strict=True)
            except (FileNotFoundError, OSError, RuntimeError):
                continue
            if stat.S_ISREG(mode) and resolved.is_relative_to(root):
                inventory.append(candidate.relative_to(root).as_posix())
    return sorted(set(inventory))


def _path_from_engine_text(text: str, cwd: Path) -> Path | None:
    if text.startswith("file:"):
        parsed = urlparse(text)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            return None
        return Path(unquote(parsed.path))
    path = Path(text)
    return path if path.is_absolute() else cwd / path


def _workspace_relative_engine_path(text: str, root: Path) -> str | None:
    path = _path_from_engine_text(text, root)
    if path is None:
        return None
    # Do not resolve here. A lexical in-root symlink must remain visible so the
    # trust comparison rejects it rather than converting it to an external import.
    absolute = Path(os.path.abspath(path))
    if not absolute.is_relative_to(root):
        return None
    if absolute.suffix.lower() not in SUPPORTED_PYTHON:
        return None
    return absolute.relative_to(root).as_posix()


def parse_pyright_dependency_output(output: str, root: Path) -> dict[str, Any]:
    """Parse the pinned public CLI's selected config and file graph.

    A graph node is a non-indented path immediately followed by ``Imports``.
    Pyright emits source roots without graph edges in its final
    ``files not explicitly imported`` block, so those paths are included too.
    """
    root = root.resolve()
    lines = output.splitlines()
    program: set[str] = set()
    for index, line in enumerate(lines[:-1]):
        if line[:1].isspace() or not re.fullmatch(r" Imports +\d+ files?", lines[index + 1]):
            continue
        normalized = _workspace_relative_engine_path(line, root)
        if normalized is not None:
            program.add(normalized)

    in_unimported = False
    for line in lines:
        if re.fullmatch(r"\d+ files? not explicitly imported", line):
            in_unimported = True
            continue
        if not in_unimported:
            continue
        if not line.startswith("    "):
            if line:
                in_unimported = False
            continue
        normalized = _workspace_relative_engine_path(line.strip(), root)
        if normalized is not None:
            program.add(normalized)

    config_candidates: list[Path] = []
    for pattern in (
        r"^Loading configuration file at (.+)$",
        r"^Loading pyproject\.toml file at (.+)$",
    ):
        for match in re.finditer(pattern, output, flags=re.MULTILINE):
            config_candidates.append(Path(match.group(1).strip()))
    selected_config_path: str | None = None
    if config_candidates:
        selected = config_candidates[-1].resolve()
        if not selected.is_relative_to(root):
            raise RuntimeError(f"Pyright selected a config outside the workspace: {selected}")
        selected_config_path = selected.relative_to(root).as_posix()

    source_matches = re.findall(r"^Found (\d+) source files?$", output, flags=re.MULTILINE)
    version_matches = re.findall(r"^pyright ([^\s]+)$", output, flags=re.MULTILINE)
    return {
        "selected_config_path": selected_config_path,
        "project_kind": "configured" if selected_config_path is not None else "workspace_default",
        "configured_source_count": int(source_matches[-1]) if source_matches else None,
        "engine_version": version_matches[-1] if version_matches else None,
        "configured_program_paths": sorted(program),
    }


def _path_has_symlink(root: Path, relative: str) -> bool:
    current = root
    for part in Path(relative).parts:
        current /= part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return True
        except OSError:
            return False
    return False


def _outside_reason(root: Path, relative: str, inventory_kind: str) -> str:
    if Path(relative).is_absolute():
        return "outside_workspace"
    candidate = root / relative
    if _path_has_symlink(root, relative):
        return "symlink_or_escape"
    try:
        mode = candidate.lstat().st_mode
    except OSError:
        return "missing"
    if not stat.S_ISREG(mode):
        return "non_regular"
    try:
        if not candidate.resolve(strict=True).is_relative_to(root):
            return "symlink_or_escape"
    except (OSError, RuntimeError):
        return "symlink_or_escape"
    if inventory_kind == "git":
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", "--", relative],
            cwd=root,
            check=False,
        )
        return "git_ignored" if ignored.returncode == 0 else "not_in_git_inventory"
    return "excluded_from_bounded_inventory"


def classify_scope(
    *,
    root: Path,
    inventory_kind: str,
    trust_inventory: list[str],
    parsed: dict[str, Any],
) -> dict[str, Any]:
    program = list(parsed["configured_program_paths"])
    trust = set(trust_inventory)
    program_set = set(program)
    trusted_not_in_program = sorted(trust - program_set)
    outside_trust = sorted(program_set - trust)
    source_count = parsed["configured_source_count"]
    evidence_reasons: list[str] = []
    if source_count is None:
        evidence_reasons.append("missing_pyright_source_count")
    elif len(program) < source_count:
        evidence_reasons.append("dependency_graph_has_fewer_workspace_paths_than_reported_sources")
    if parsed["engine_version"] != EXPECTED_VERSIONS["pyright"]:
        evidence_reasons.append("unexpected_pyright_version")

    scope_compatible = not outside_trust and not evidence_reasons
    error: dict[str, Any] | None = None
    if not scope_compatible:
        error = {
            "code": SCOPE_INCOMPATIBLE,
            "message": "Pyright configured-program attribution is outside trust or incomplete",
            "paths": outside_trust,
            "evidence_reasons": evidence_reasons,
        }
    return {
        **parsed,
        "trust_inventory_kind": inventory_kind,
        "trust_inventory_paths": trust_inventory,
        "trust_inventory_count": len(trust_inventory),
        "trust_inventory_digest": path_digest(trust_inventory),
        "configured_program_count": len(program),
        "configured_program_digest": path_digest(program),
        "configured_program_evidence": {
            "kind": "pyright_cli_dependencies_stats",
            "comparison_basis": "normalized_path_sets",
            "count_only_equivalence_rejected": True,
            "projection_complete": not evidence_reasons,
            "evidence_reasons": evidence_reasons,
        },
        "trusted_not_in_configured_program": trusted_not_in_program,
        "configured_program_outside_trust": outside_trust,
        "difference_reasons": {
            "trusted_not_in_configured_program": [
                {
                    "path": path,
                    "reason": (
                        "omitted_by_native_config_or_engine_program"
                        if parsed["selected_config_path"] is not None
                        else "omitted_by_engine_workspace_program"
                    ),
                }
                for path in trusted_not_in_program
            ],
            "configured_program_outside_trust": [
                {"path": path, "reason": _outside_reason(root, path, inventory_kind)} for path in outside_trust
            ],
        },
        "scope_compatible": scope_compatible,
        "error": error,
        "status": "pass" if scope_compatible else "fail",
    }


def probe_pyright_scope(
    root: Path,
    interpreter: Path,
    node: Path,
    pyright: Path,
    *,
    timeout: float = 90.0,
) -> dict[str, Any]:
    """Run the pinned engine's native program and return path-level attribution."""
    root = root.resolve()
    git_top = _git_toplevel(root)
    if git_top == root:
        inventory_kind = "git"
        inventory = git_trust_inventory(root)
    else:
        inventory_kind = "bounded_no_symlink"
        inventory = bounded_trust_inventory(root)

    owned_files_probe = Path(__file__).with_name("pyright_owned_files_probe.mjs")
    command = [
        str(node),
        "--",
        str(owned_files_probe),
        "--pythonpath",
        str(interpreter),
        "-p",
        str(root),
    ]
    env = os.environ.copy()
    env["PATH"] = str(node.parent)
    env.pop("NODE_PATH", None)
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        output = completed.stdout + completed.stderr
        raise RuntimeError(f"Pyright scope command failed with exit {completed.returncode}: {output[-2000:]}")
    try:
        native = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Pyright owned-files probe returned invalid JSON: {exc}") from exc
    if not isinstance(native, dict) or native.get("schema_version") != 1:
        raise RuntimeError("Pyright owned-files probe returned an unsupported schema")
    engine = native.get("engine")
    project = native.get("project")
    owned_files = native.get("owned_files")
    if not isinstance(engine, dict) or not isinstance(project, dict) or not isinstance(owned_files, list):
        raise RuntimeError("Pyright owned-files probe omitted required engine, project, or path evidence")
    if engine.get("version") != EXPECTED_VERSIONS["pyright"]:
        raise RuntimeError(f"Pyright owned-files probe version drifted: {engine.get('version')!r}")
    if Path(str(engine.get("cli_entrypoint"))).resolve() != pyright.resolve():
        raise RuntimeError("Pyright owned-files probe did not use the locked CLI entrypoint")
    absolute_paths = sorted(map(str, owned_files))
    if native.get("owned_file_count") != len(absolute_paths) or native.get("owned_files_sha256") != path_digest(
        absolute_paths
    ):
        raise RuntimeError("Pyright owned-files probe count or digest does not match its path evidence")
    program_paths: list[str] = []
    for raw in absolute_paths:
        path = Path(raw)
        if not path.is_absolute() or "\0" in raw:
            raise RuntimeError(f"Pyright owned-files probe returned an invalid absolute path: {raw!r}")
        lexical = Path(os.path.abspath(path))
        if lexical.suffix.lower() not in SUPPORTED_PYTHON:
            continue
        program_paths.append(
            lexical.relative_to(root).as_posix() if lexical.is_relative_to(root) else lexical.as_posix()
        )
    program_paths = sorted(set(program_paths))
    selected_config = project.get("selected_config_path")
    if selected_config is not None:
        selected = Path(str(selected_config)).resolve()
        if not selected.is_relative_to(root):
            raise RuntimeError(f"Pyright selected a config outside the workspace: {selected}")
        selected_config = selected.relative_to(root).as_posix()
    project_kind = project.get("project_kind")
    if project_kind not in {"configured", "workspace_default"}:
        raise RuntimeError(f"Pyright owned-files probe returned an invalid project kind: {project_kind!r}")
    if (project_kind == "configured") is (selected_config is None):
        raise RuntimeError("Pyright owned-files probe returned inconsistent native config evidence")
    parsed = {
        "selected_config_path": selected_config,
        "project_kind": project_kind,
        "configured_source_count": len(program_paths),
        "engine_version": engine["version"],
        "configured_program_paths": program_paths,
    }
    result = classify_scope(
        root=root,
        inventory_kind=inventory_kind,
        trust_inventory=inventory,
        parsed=parsed,
    )
    result["engine_command"] = {
        "executable": str(pyright),
        "options": [
            "pyright_owned_files_probe.mjs",
            "--pythonpath",
            "<PINNED_MS_PYTHON>",
            "-p",
            "<WORKSPACE>",
        ],
        "exit_code": completed.returncode,
    }
    result["configured_program_evidence"].update(
        {
            "kind": "pinned_pyright_analyzer_service_owned_files",
            "native_attribution": native.get("attribution"),
            "project_attribution": project.get("attribution"),
            "bundle": native.get("bundle"),
        }
    )
    result["projection_seconds"] = native.get("elapsed_seconds", round(time.monotonic() - started, 3))
    result["projection_max_rss_kib"] = native.get("max_rss_kib")
    result["overlay_generated"] = False
    return result
