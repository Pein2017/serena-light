from __future__ import annotations

import os
import subprocess
from pathlib import Path

from scripts.external_snapshot import snapshot_identity


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


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
