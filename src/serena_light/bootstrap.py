"""Materialize and verify the repository-locked serena-light runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

LOCK_INPUTS = ("pyproject.toml", "uv.lock", "package.json", "package-lock.json")
RUNTIME_BASE = Path("/data/CoordExp/.codex/runtime/serena-light/deps")
NODE_VERSION = "22.22.0"
EXPECTED_VERSIONS = {
    "npm": "11.13.0",
    "pyright": "1.1.403",
    "typescript-language-server": "5.1.3",
    "typescript": "5.9.3",
}
FORBIDDEN_ENGINE_PREFIXES = (Path("/root/.nvm"),)
FORBIDDEN_PYTHON_IMPORT_PREFIXES = (Path("/root/.local/lib"),)


class BootstrapError(RuntimeError):
    """Raised when the locked runtime cannot be proven self-contained."""


def repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def lock_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for name in LOCK_INPUTS:
        path = root / name
        if not path.is_file():
            raise BootstrapError(f"missing lock input: {path}")
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def runtime_paths(root: Path) -> dict[str, Path]:
    runtime = RUNTIME_BASE / lock_digest(root)
    node_root = runtime / "node"
    packages = runtime / "node-packages"
    return {
        "runtime": runtime,
        "python": runtime / "python" / "bin" / "python",
        "node": node_root / "bin" / "node",
        "npm": packages / "node_modules" / "npm" / "bin" / "npm-cli.js",
        "pyright": packages / "node_modules" / "pyright" / "index.js",
        "pyright-langserver": packages / "node_modules" / "pyright" / "langserver.index.js",
        "typescript-language-server": packages / "node_modules" / "typescript-language-server" / "lib" / "cli.mjs",
        "tsserver": packages / "node_modules" / "typescript" / "lib" / "tsserver.js",
        "typescript": packages / "node_modules" / "typescript" / "lib" / "tsc.js",
    }


def _run(command: list[str], *, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(command, check=False, capture_output=True, text=True, env=env)
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise BootstrapError(f"command failed ({completed.returncode}): {command!r}: {detail}")
    return (completed.stdout or completed.stderr).strip()


def _find_uv() -> Path:
    adjacent = Path(sys.executable).with_name("uv")
    candidate = adjacent if adjacent.is_file() else Path(shutil.which("uv") or "")
    if not candidate.is_file():
        raise BootstrapError("uv is required to materialize the locked Python environment")
    return candidate.resolve()


def materialize(root: Path, paths: dict[str, Path]) -> None:
    runtime = paths["runtime"]
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    uv = _find_uv()
    sync_env = os.environ.copy()
    sync_env["UV_PROJECT_ENVIRONMENT"] = str(runtime / "python")
    _run([str(uv), "sync", "--frozen", "--all-extras", "--project", str(root)], env=sync_env)

    if not paths["node"].is_file():
        _run(
            [
                str(paths["python"]),
                "-m",
                "nodeenv",
                "--node",
                NODE_VERSION,
                "--prebuilt",
                str(runtime / "node"),
            ]
        )

    package_root = runtime / "node-packages"
    package_root.mkdir(exist_ok=True)
    shutil.copy2(root / "package.json", package_root / "package.json")
    shutil.copy2(root / "package-lock.json", package_root / "package-lock.json")
    install_env = os.environ.copy()
    install_env["PATH"] = str(paths["node"].parent)
    bootstrap_npm = runtime / "node" / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    _run(
        [str(paths["node"]), str(bootstrap_npm), "ci", "--ignore-scripts", "--prefix", str(package_root)],
        env=install_env,
    )


def _assert_owned(path: Path, runtime: Path, *, allow_interpreter_symlink: bool = False) -> None:
    resolved = path.resolve()
    if not path.is_file():
        raise BootstrapError(f"missing runtime executable/module: {path}")
    lexical = path.absolute()
    if not lexical.is_relative_to(runtime.resolve()):
        raise BootstrapError(f"runtime path escaped lock directory: {resolved}")
    if not allow_interpreter_symlink and not resolved.is_relative_to(runtime.resolve()):
        raise BootstrapError(f"runtime module resolves outside lock directory: {resolved}")
    if any(resolved.is_relative_to(prefix) for prefix in FORBIDDEN_ENGINE_PREFIXES):
        raise BootstrapError(f"forbidden global runtime path: {resolved}")


def inspect_runtime(root: Path) -> dict[str, Any]:
    paths = runtime_paths(root)
    runtime = paths["runtime"]
    for name, path in paths.items():
        if name != "runtime":
            _assert_owned(path, runtime, allow_interpreter_symlink=name == "python")

    versions = {
        "python": _run([str(paths["python"]), "--version"]),
        "node": _run([str(paths["node"]), "--version"]).removeprefix("v"),
        "npm": _run([str(paths["node"]), str(paths["npm"]), "--version"]),
        "pyright": _run([str(paths["node"]), str(paths["pyright"]), "--version"]).split()[-1],
        "typescript-language-server": _run([str(paths["node"]), str(paths["typescript-language-server"]), "--version"]),
        "typescript": _run([str(paths["node"]), str(paths["typescript"]), "--version"]).split()[-1],
    }
    if versions["node"] != NODE_VERSION:
        raise BootstrapError(f"node version drift: {versions['node']} != {NODE_VERSION}")
    for name, expected in EXPECTED_VERSIONS.items():
        if versions[name] != expected:
            raise BootstrapError(f"{name} version drift: {versions[name]} != {expected}")
    python_probe = json.loads(
        _run(
            [
                str(paths["python"]),
                "-c",
                "import json,mcp,lsprotocol; print(json.dumps([mcp.__file__,lsprotocol.__file__]))",
            ]
        )
    )
    for imported in python_probe:
        _assert_owned(Path(imported), runtime)
        resolved_import = Path(imported).resolve()
        if any(resolved_import.is_relative_to(prefix) for prefix in FORBIDDEN_PYTHON_IMPORT_PREFIXES):
            raise BootstrapError(f"forbidden user-site Python import: {resolved_import}")
    return {
        "lock_digest": lock_digest(root),
        "runtime": str(runtime),
        "paths": {name: str(path.resolve()) for name, path in paths.items() if name != "runtime"},
        "versions": versions,
        "python_imports": python_probe,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify without materializing")
    parser.add_argument("--json", action="store_true", help="emit machine-readable status")
    parser.add_argument("--output", type=Path, help="write the JSON status to this path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = repository_root()
    try:
        paths = runtime_paths(root)
        if not args.check:
            materialize(root, paths)
        report = inspect_runtime(root)
    except (BootstrapError, subprocess.CalledProcessError) as exc:
        print(f"serena-light bootstrap failed: {exc}", file=sys.stderr)
        return 1
    rendered_json = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered_json, encoding="utf-8")
    if args.json:
        print(rendered_json, end="")
    else:
        print(f"lock_digest: {report['lock_digest']}")
        print(f"runtime: {report['runtime']}")
        for name, path in report["paths"].items():
            print(f"{name}: {path}")
        for name, version in report["versions"].items():
            print(f"{name}_version: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
