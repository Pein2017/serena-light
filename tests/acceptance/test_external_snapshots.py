from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import ClassVar, cast

import pytest

from scripts.external_snapshot import (
    CC_PLUGIN_CODEX_TYPESCRIPT_AUTHORITY_PROFILE,
    _node_platform_architecture,
    snapshot_identity,
)


def _acceptance_conftest() -> ModuleType:
    module_name = "_serena_light_acceptance_conftest"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    path = Path(__file__).resolve().parents[1] / "conftest.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


class _ExternalMarkerItem:
    def __init__(self, *, root: Path, environment_name: str) -> None:
        self._marker = pytest.mark.external_repo(root=str(root), snapshot_env=environment_name)
        self.nodeid = "acceptance::external-marker-gate"

    def iter_markers(self, *, name: str):
        return (self._marker,) if name == "external_repo" else ()


def test_missing_snapshot_environment_skips_without_resolving_the_marker_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environment_name = "SERENA_LIGHT_TEST_MISSING_SNAPSHOT"
    missing_root = tmp_path / "does-not-exist"
    monkeypatch.delenv(environment_name, raising=False)
    acceptance_conftest = _acceptance_conftest()

    def fail_if_called(*args: object, **kwargs: object) -> str:
        del args, kwargs
        raise AssertionError("the no-opt-in marker gate must not resolve its external root")

    monkeypatch.setattr(acceptance_conftest, "snapshot_identity", fail_if_called)
    with pytest.raises(pytest.skip.Exception, match=environment_name):
        acceptance_conftest.pytest_runtest_setup(
            cast(pytest.Item, _ExternalMarkerItem(root=missing_root, environment_name=environment_name))
        )


def test_explicit_snapshot_opt_in_fails_when_the_marker_root_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    environment_name = "SERENA_LIGHT_TEST_MISSING_SNAPSHOT"
    monkeypatch.setenv(environment_name, "git:expected:identity")
    acceptance_conftest = _acceptance_conftest()
    with pytest.raises(FileNotFoundError):
        acceptance_conftest.pytest_runtest_setup(
            cast(pytest.Item, _ExternalMarkerItem(root=tmp_path / "does-not-exist", environment_name=environment_name))
        )


def test_snapshot_teardown_reuses_the_profile_selected_during_setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    acceptance_conftest = _acceptance_conftest()
    environment_name = "SERENA_LIGHT_CC_PLUGIN_CODEX_SNAPSHOT"
    profile = "cc-plugin-codex-typescript-authority-v1"
    observed = "git:head:authority-bound"
    gate = acceptance_conftest._SnapshotGate(tmp_path, environment_name, profile, observed)

    class Item:
        stash: ClassVar[dict[object, tuple[object, ...]]] = {
            acceptance_conftest.SNAPSHOT_GATES_KEY: (gate,)
        }
        nodeid = "acceptance::profile-stability"

    calls: list[tuple[Path, str]] = []

    def snapshot(root: Path, *, profile: str) -> str:
        calls.append((root, profile))
        return observed

    monkeypatch.setenv(environment_name, observed)
    monkeypatch.setattr(acceptance_conftest, "snapshot_identity", snapshot)
    acceptance_conftest.pytest_runtest_teardown(cast(pytest.Item, Item()), None)
    assert calls == [(tmp_path, profile)]


def test_git_snapshot_binds_head_binary_diff_and_untracked_regular_and_symlink_content(tmp_path: Path) -> None:
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "config", "user.email", "acceptance@example.invalid")
    _git(tmp_path, "config", "user.name", "Acceptance")
    tracked = tmp_path / "tracked.bin"
    tracked.write_bytes(b"before\x00")
    _git(tmp_path, "add", "tracked.bin")
    _git(tmp_path, "commit", "--quiet", "-m", "initial")

    tracked.write_bytes(b"after\x00")
    untracked = tmp_path / "untracked.bin"
    untracked.write_bytes(b"one")
    link = tmp_path / "untracked-link"
    link.symlink_to("untracked.bin")
    first = snapshot_identity(tmp_path)

    untracked.write_bytes(b"two")
    assert snapshot_identity(tmp_path) != first
    second = snapshot_identity(tmp_path)
    link.unlink()
    link.symlink_to("tracked.bin")
    assert snapshot_identity(tmp_path) != second


def test_non_git_transformers_snapshot_binds_source_content_and_package_metadata(tmp_path: Path) -> None:
    root = tmp_path / "transformers"
    root.mkdir()
    (root / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    dist_info = tmp_path / "transformers-9.9.9.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text("Name: transformers\nVersion: 9.9.9\n", encoding="utf-8")
    first = snapshot_identity(root)

    (root / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert snapshot_identity(root) != first

    # Bytecode is runtime debris, not source-tree identity.
    cache = root / "__pycache__"
    cache.mkdir()
    (cache / "module.cpython-312.pyc").write_bytes(os.urandom(8))
    second = snapshot_identity(root)
    (cache / "module.cpython-312.pyc").write_bytes(os.urandom(8))
    assert snapshot_identity(root) == second


def test_cc_plugin_typescript_profile_binds_the_ignored_native_tsc_executable(tmp_path: Path) -> None:
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "config", "user.email", "acceptance@example.invalid")
    _git(tmp_path, "config", "user.name", "Acceptance")
    (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"scripts":{"typecheck":"tsc -p tsconfig.json"}}\n', encoding="utf-8")
    (tmp_path / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    _git(tmp_path, "add", ".gitignore", "package.json", "package-lock.json")
    _git(tmp_path, "commit", "--quiet", "-m", "initial")

    node_modules = tmp_path / "node_modules"
    typescript = node_modules / "typescript"
    (typescript / "bin").mkdir(parents=True)
    (typescript / "lib").mkdir()
    (node_modules / ".package-lock.json").write_text('{"packages":{}}\n', encoding="utf-8")
    (typescript / "package.json").write_text('{"name":"typescript","version":"7.0.2"}\n', encoding="utf-8")
    (typescript / "bin/tsc").write_text('#!/usr/bin/env node\nimport "../lib/tsc.js";\n', encoding="utf-8")
    (typescript / "lib/tsc.js").write_text('import getExePath from "#getExePath";\n', encoding="utf-8")
    (typescript / "lib/getExePath.js").write_text("export default function getExePath() {}\n", encoding="utf-8")
    bin_directory = node_modules / ".bin"
    bin_directory.mkdir()
    (bin_directory / "tsc").symlink_to("../typescript/bin/tsc")
    platform_package = node_modules / "@typescript" / f"typescript-{_node_platform_architecture(tmp_path)}"
    (platform_package / "lib").mkdir(parents=True)
    (platform_package / "package.json").write_text(
        '{"name":"@typescript/typescript-platform","version":"7.0.2"}\n', encoding="utf-8"
    )
    native_tsc = platform_package / "lib/tsc"
    native_tsc.write_bytes(b"native tsc version one")

    first = snapshot_identity(tmp_path, profile=CC_PLUGIN_CODEX_TYPESCRIPT_AUTHORITY_PROFILE)
    native_tsc.write_bytes(b"native tsc version two")
    second = snapshot_identity(tmp_path, profile=CC_PLUGIN_CODEX_TYPESCRIPT_AUTHORITY_PROFILE)
    assert second != first
    (typescript / "bin/tsc").write_text('#!/usr/bin/env node\nimport "../lib/changed.js";\n', encoding="utf-8")
    assert snapshot_identity(tmp_path, profile=CC_PLUGIN_CODEX_TYPESCRIPT_AUTHORITY_PROFILE) != second
