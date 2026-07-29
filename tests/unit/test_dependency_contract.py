"""Regression checks for the repository-owned dependency baseline."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from serena_light.source_budget import inspect_dependency_boundary

ROOT = Path(__file__).resolve().parents[2]
PYTHON_BASELINE = {
    "anyio": "4.14.2",
    "httpx": "0.28.1",
    "mcp": "1.27.1",
    "lsprotocol": "2025.0.0",
    "starlette": "1.3.1",
    "uvicorn": "0.51.0",
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


def test_only_the_connector_has_a_public_runtime_entrypoint() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = project["project"]["scripts"]

    assert scripts["serena-light"] == "serena_light.cli:connector_main"
    assert "serena-light-daemon" not in scripts


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


def test_direct_external_imports_have_direct_dependency_owners() -> None:
    report = inspect_dependency_boundary(ROOT)

    assert report["direct_external_imports"] == [
        "anyio",
        "httpx",
        "lsprotocol",
        "mcp",
        "psutil",
        "pydantic",
        "starlette",
        "uvicorn",
    ]
    assert report["undeclared_external_imports"] == []


def test_dependency_boundary_excludes_stdlib_and_internal_imports(tmp_path: Path) -> None:
    package = tmp_path / "src" / "serena_light"
    package.mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "0"\ndependencies = ["owned-package==1.0"]\n',
        encoding="utf-8",
    )
    (package / "example.py").write_text(
        "import json\nfrom serena_light import sibling\nimport owned_package\nimport undeclared\n",
        encoding="utf-8",
    )

    report = inspect_dependency_boundary(tmp_path)

    assert report["direct_external_imports"] == ["owned_package", "undeclared"]
    assert report["undeclared_external_imports"] == ["undeclared"]
