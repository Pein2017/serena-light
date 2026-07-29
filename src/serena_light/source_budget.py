"""Validate source ownership, dependencies, and pinned Serena provenance."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

MAX_PRODUCTION_LINES = None
SERENA_REFERENCE_ROOT = Path("/data/CoordExp/external/serena")
SERENA_REFERENCE_COMMIT = "9a9d07e83d8c1cba3458992707f440c624446c6d"
SERENA_REFERENCE_LICENSE = "MIT"
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
DEPENDENCY_NAME = re.compile(r"^\s*([A-Za-z0-9_.-]+)")


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


def _symbol_sha256(source: Path, symbol: str) -> str:
    available = _qualified_symbols(source)
    if symbol not in available:
        raise CensusError(f"unknown copied symbol {symbol!r} in {source}")
    start, end = available[symbol]
    lines = source.read_bytes().splitlines(keepends=True)
    return hashlib.sha256(b"".join(lines[start - 1 : end])).hexdigest()


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


def _declared_dependencies(root: Path) -> set[str]:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared: set[str] = set()
    for requirement in project.get("dependencies", []):
        match = DEPENDENCY_NAME.match(requirement)
        if match is None:
            raise CensusError(f"invalid project dependency declaration: {requirement!r}")
        declared.add(match.group(1).lower().replace("_", "-"))
    return declared


def inspect_dependency_boundary(root: Path) -> dict[str, Any]:
    """Report direct external imports and require direct project ownership."""
    source_root = root / "src"
    internal_modules = {
        path.stem if path.is_file() else path.name
        for path in source_root.iterdir()
        if path.is_dir() or path.suffix == ".py"
    }
    external_imports: dict[str, list[str]] = {}
    for path in sorted(source_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported = [node.module]
            else:
                continue
            for module in imported:
                top_level = module.split(".", maxsplit=1)[0]
                if top_level in sys.stdlib_module_names or top_level in internal_modules:
                    continue
                external_imports.setdefault(top_level, []).append(
                    f"{path.relative_to(root)}:{node.lineno}:{module}"
                )

    declared = _declared_dependencies(root)
    undeclared = sorted(
        module
        for module in external_imports
        if module.lower().replace("_", "-") not in declared
    )
    return {
        "declared_dependencies": sorted(declared),
        "direct_external_imports": sorted(external_imports),
        "import_locations": {module: sorted(locations) for module, locations in sorted(external_imports.items())},
        "undeclared_external_imports": undeclared,
    }


def _inspect_copied_manifest(root: Path, reference: Path) -> tuple[set[tuple[str, str]], int]:
    manifest = json.loads((root / "third_party" / "copied_sources.json").read_text(encoding="utf-8"))
    expected_reference = {
        "repository": str(SERENA_REFERENCE_ROOT),
        "commit": SERENA_REFERENCE_COMMIT,
        "license": SERENA_REFERENCE_LICENSE,
    }
    if manifest.get("reference") != expected_reference:
        raise CensusError(f"copied-source reference does not match the official pin: {manifest.get('reference')!r}")

    required_fields = set(manifest.get("required_entry_fields", []))
    expected_fields = {"source_path", "source_symbol", "license", "copied_sha256", "local_owner"}
    if required_fields != expected_fields:
        raise CensusError(f"copied-source required fields drifted: {sorted(required_fields)}")

    copied: set[tuple[str, str]] = set()
    for entry in manifest.get("copies", []):
        missing_fields = expected_fields - entry.keys()
        if missing_fields:
            raise CensusError(f"copied-source entry omitted fields: {sorted(missing_fields)}")
        key = (entry["source_path"], entry["source_symbol"])
        if key in copied:
            raise CensusError(f"duplicate copied-source entry: {key}")
        copied.add(key)
        if entry["license"] != SERENA_REFERENCE_LICENSE:
            raise CensusError(f"copied-source entry lost MIT provenance: {key}")
        owner = root / entry["local_owner"]
        if not owner.is_file():
            raise CensusError(f"missing local owner for copied source {key}: {owner}")
        source = reference / entry["source_path"]
        if not source.is_file():
            raise CensusError(f"missing copied reference source: {source}")
        actual_hash = _symbol_sha256(source, entry["source_symbol"])
        if actual_hash != entry["copied_sha256"]:
            raise CensusError(
                f"copied-source hash drift for {key}: {actual_hash} != {entry['copied_sha256']}"
            )
    return copied, len(copied)


def inspect_census(root: Path) -> dict[str, Any]:
    census = json.loads((root / "third_party" / "serena_source_census.json").read_text())
    reference = Path(census["reference_root"])
    expected_commit = census["commit"]
    if reference != SERENA_REFERENCE_ROOT:
        raise CensusError(f"census reference root drift: {reference} != {SERENA_REFERENCE_ROOT}")
    if expected_commit != SERENA_REFERENCE_COMMIT:
        raise CensusError(f"census commit drift: {expected_commit} != {SERENA_REFERENCE_COMMIT}")
    actual_commit = _git_commit(reference)
    if actual_commit != expected_commit:
        raise CensusError(f"Serena reference drift: {actual_commit} != {expected_commit}")
    excluded_subsystems = set(census.get("excluded_subsystems", []))
    missing_forbidden = REQUIRED_FORBIDDEN_SUBSYSTEMS - excluded_subsystems
    if missing_forbidden:
        raise CensusError(f"census omitted forbidden subsystems: {sorted(missing_forbidden)}")

    manifest_copies, verified_hashes = _inspect_copied_manifest(root, reference)
    census_copies: set[tuple[str, str]] = set()
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
            if action == "copy":
                census_copies.update((source_path, symbol) for symbol in symbols)
            if action in {"copy", "reshape"}:
                selected_total += _selected_lines(source, symbols)

    missing_manifest_entries = census_copies - manifest_copies
    unclassified_manifest_entries = manifest_copies - census_copies
    if missing_manifest_entries or unclassified_manifest_entries:
        raise CensusError(
            "census/manifest copy classifications disagree: "
            f"missing_manifest_entries={sorted(missing_manifest_entries)}, "
            f"unclassified_manifest_entries={sorted(unclassified_manifest_entries)}"
        )

    local_lines = _production_lines(root)
    forbidden_imports = inspect_import_boundary(root)
    if forbidden_imports:
        raise CensusError(f"forbidden Serena/SolidLSP runtime imports entered the core: {forbidden_imports}")
    dependency_boundary = inspect_dependency_boundary(root)
    if dependency_boundary["undeclared_external_imports"]:
        raise CensusError(
            "direct external imports lack project dependency ownership: "
            f"{dependency_boundary['undeclared_external_imports']}"
        )
    estimate = selected_total + census["owned_code_estimate_lines"]
    return {
        "reference_commit": actual_commit,
        "selected_upstream_lines": selected_total,
        "owned_code_estimate_lines": census["owned_code_estimate_lines"],
        "expected_production_lines": estimate,
        "current_local_production_lines": local_lines,
        "maximum_production_lines": MAX_PRODUCTION_LINES,
        "action_counts": action_counts,
        "forbidden_imports": forbidden_imports,
        "dependency_boundary": dependency_boundary,
        "copied_source_hashes_verified": verified_hashes,
        "census_manifest_agreement": True,
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
