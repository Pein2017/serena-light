"""Validate the Serena source census and the 12k production-code stop gate."""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

MAX_PRODUCTION_LINES = 12_000
REQUIRED_FORBIDDEN_SUBSYSTEMS = {
    "src/serena/agent.py",
    "src/serena/project_server.py",
    "src/serena/memories/",
    "src/serena/resources/config/modes/",
    "src/serena/resources/dashboard/",
    "src/serena/dashboard.py",
    "src/serena/gui_log_viewer.py",
    "src/serena/jetbrains/",
}
FORBIDDEN_RUNTIME_IMPORT_PREFIXES = ("serena", "solidlsp")


class CensusError(RuntimeError):
    """Raised when provenance or the source budget is invalid."""


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _git_commit(reference: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=reference, check=True, capture_output=True, text=True
    ).stdout.strip()


def _qualified_symbols(source: Path) -> dict[str, tuple[int, int]]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    symbols: dict[str, tuple[int, int]] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            symbols[node.name] = (node.lineno, node.end_lineno or node.lineno)
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                        symbols[f"{node.name}.{child.name}"] = (
                            child.lineno,
                            child.end_lineno or child.lineno,
                        )
    return symbols


def _selected_lines(source: Path, selected: list[str]) -> int:
    available = _qualified_symbols(source)
    ranges: set[int] = set()
    for symbol in selected:
        if symbol not in available:
            raise CensusError(f"unknown census symbol {symbol!r} in {source}")
        start, end = available[symbol]
        ranges.update(range(start, end + 1))
    return len(ranges)


def _production_lines(root: Path) -> int:
    count = 0
    production_extensions = {".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
    for path in (root / "src").rglob("*"):
        if not path.is_file() or path.suffix not in production_extensions:
            continue
        count += sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    return count


def inspect_import_boundary(root: Path) -> list[str]:
    """Return forbidden runtime imports from the independent Serena Light core."""
    violations: list[str] = []
    for path in sorted((root / "src" / "serena_light").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]
            else:
                continue
            for module in imported:
                if any(
                    module == prefix or module.startswith(prefix + ".") for prefix in FORBIDDEN_RUNTIME_IMPORT_PREFIXES
                ):
                    violations.append(f"{path.relative_to(root)}:{node.lineno}:{module}")
    return violations


def inspect_census(root: Path) -> dict[str, Any]:
    census = json.loads((root / "third_party" / "serena_source_census.json").read_text())
    reference = Path(census["reference_root"])
    expected_commit = census["commit"]
    actual_commit = _git_commit(reference)
    if actual_commit != expected_commit:
        raise CensusError(f"Serena reference drift: {actual_commit} != {expected_commit}")
    excluded_subsystems = set(census.get("excluded_subsystems", []))
    missing_forbidden = REQUIRED_FORBIDDEN_SUBSYSTEMS - excluded_subsystems
    if missing_forbidden:
        raise CensusError(f"census omitted forbidden subsystems: {sorted(missing_forbidden)}")

    selected_total = 0
    action_counts = {"copy": 0, "reshape": 0, "reference": 0, "delete": 0}
    for entry in census["files"]:
        source_path = entry["source_path"]
        if any(
            source_path == forbidden.rstrip("/") or source_path.startswith(forbidden)
            for forbidden in excluded_subsystems
        ):
            raise CensusError(f"forbidden subsystem entered source closure: {source_path}")
        source = reference / source_path
        if not source.is_file():
            raise CensusError(f"missing reference source: {source}")
        classified: set[str] = set()
        for action in action_counts:
            symbols = entry.get(action, [])
            overlap = classified.intersection(symbols)
            if overlap:
                raise CensusError(f"symbols have duplicate census actions in {source_path}: {sorted(overlap)}")
            classified.update(symbols)
            action_counts[action] += len(symbols)
            if action in {"copy", "reshape"}:
                selected_total += _selected_lines(source, symbols)

    local_lines = _production_lines(root)
    forbidden_imports = inspect_import_boundary(root)
    if forbidden_imports:
        raise CensusError(f"forbidden Serena/SolidLSP runtime imports entered the core: {forbidden_imports}")
    estimate = selected_total + census["owned_code_estimate_lines"]
    if estimate > MAX_PRODUCTION_LINES:
        raise CensusError(f"expected production code {estimate} exceeds {MAX_PRODUCTION_LINES}")
    if local_lines > MAX_PRODUCTION_LINES:
        raise CensusError(f"local production code {local_lines} exceeds {MAX_PRODUCTION_LINES}")
    return {
        "reference_commit": actual_commit,
        "selected_upstream_lines": selected_total,
        "owned_code_estimate_lines": census["owned_code_estimate_lines"],
        "expected_production_lines": estimate,
        "current_local_production_lines": local_lines,
        "maximum_production_lines": MAX_PRODUCTION_LINES,
        "action_counts": action_counts,
        "forbidden_imports": forbidden_imports,
        "status": "pass",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = inspect_census(repository_root())
    except (CensusError, OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"source budget failed: {exc}", file=sys.stderr)
        return 1
    rendered_json = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered_json, encoding="utf-8")
    if args.json:
        print(rendered_json, end="")
    else:
        for key, value in report.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
