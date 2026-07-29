"""Regression checks for the repository-owned dependency baseline."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON_BASELINE = {
    "mcp": "1.27.1",
    "lsprotocol": "2025.0.0",
}
NODE_BASELINE = {
    "npm": "11.13.0",
    "pyright": "1.1.403",
    "typescript-language-server": "5.1.3",
    "typescript": "5.9.3",
}


def _uv_packages() -> dict[str, str]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {package["name"]: package["version"] for package in lock["package"]}


def test_python_baseline_is_exactly_pinned_and_locked() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["requires-python"] == "==3.12.*"

    declared = dict(dependency.split("==", maxsplit=1) for dependency in project["project"]["dependencies"])
    assert {name: declared[name] for name in PYTHON_BASELINE} == PYTHON_BASELINE

    locked = _uv_packages()
    assert {name: locked[name] for name in PYTHON_BASELINE} == PYTHON_BASELINE


def test_node_baseline_is_exactly_pinned_and_lock_coherent() -> None:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))

    assert package["dependencies"] == NODE_BASELINE
    root_package = lock["packages"][""]
    assert root_package["dependencies"] == NODE_BASELINE
    assert lock["lockfileVersion"] == 3
    for name, version in NODE_BASELINE.items():
        locked_package = lock["packages"][f"node_modules/{name}"]
        assert locked_package["version"] == version
        assert locked_package["resolved"].endswith(f"/{name}-{version}.tgz")
        assert locked_package["integrity"].startswith("sha512-")
