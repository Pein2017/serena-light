from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from serena_light.workspace.inventory import (
    SUPPORTED_EXTENSIONS,
    discover_transformers_root,
    git_trust_inventory,
    transformers_trust_inventory,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test")
    return root


def test_git_inventory_uses_cached_and_nonignored_candidates_without_walking_ignored_subtrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path)
    (root / ".gitignore").write_text("ignored/\n")
    (root / "src").mkdir()
    (root / "src" / "tracked.py").write_text("x = 1\n")
    (root / "new.ts").write_text("export {}\n")
    ignored = root / "ignored"
    ignored.mkdir()
    (ignored / "never_enumerate.py").write_text("x = 1\n")
    _git(root, "add", ".gitignore", "src/tracked.py")
    _git(root, "commit", "-m", "fixture")

    def forbidden_walk(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Git inventory must not walk ignored directories")

    monkeypatch.setattr(os, "walk", forbidden_walk)
    inventory = git_trust_inventory(root)

    assert inventory.paths == ("new.ts", "src/tracked.py")
    assert inventory.count == 2
    assert inventory.contains("src/tracked.py")
    assert inventory.absolute_paths == (root / "new.ts", root / "src/tracked.py")
    assert inventory.paths_under("src") == ("src/tracked.py",)
    assert not inventory.tree.has_prefix("ignored")
    assert inventory.digest == "a8ca379de2530e0cd7c8a98bd2a7fec5214f4562a5391a2b70fb1f5d05a7d510"


def test_git_inventory_rejects_deleted_nonregular_and_symlink_candidates(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    for name in ("deleted.py", "fifo.py", "link_inside.py", "link_escape.py"):
        (root / name).write_text("x = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    (root / "deleted.py").unlink()
    (root / "fifo.py").unlink()
    os.mkfifo(root / "fifo.py")
    (root / "link_inside.py").unlink()
    (root / "link_inside.py").symlink_to("fifo.py")
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n")
    (root / "link_escape.py").unlink()
    (root / "link_escape.py").symlink_to(outside)

    inventory = git_trust_inventory(root)

    assert inventory.paths == ()
    assert {(item.path, item.reason) for item in inventory.rejected} == {
        ("deleted.py", "missing"),
        ("fifo.py", "non_regular"),
        ("link_inside.py", "symlink"),
        ("link_escape.py", "symlink_escape"),
    }
    assert {(item.path, item.reason) for item in inventory.targeted_freshness(["deleted.py", "../outside.py"])} == {
        ("deleted.py", "missing"),
        ("../outside.py", "invalid_relative_path"),
    }


def test_transformers_discovery_is_exact_and_inventory_is_bounded_no_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_packages = tmp_path / "site-packages"
    package = site_packages / "transformers"
    package.mkdir(parents=True)
    (package / "visible.py").write_text("x = 1\n")
    (package / "hidden.py").symlink_to(tmp_path / "outside.py")
    (tmp_path / "outside.py").write_text("x = 1\n")
    (site_packages / "another-package").mkdir()
    (site_packages / "another-package" / "should_not_be_seen.py").write_text("x = 1\n")

    assert discover_transformers_root([site_packages]) == package

    real_walk = os.walk

    walked = list(real_walk(package))

    def bounded_walk(top: str | os.PathLike[str], **_kwargs: object) -> Any:
        assert Path(top) == package
        return iter(walked)

    monkeypatch.setattr(os, "walk", bounded_walk)
    inventory = transformers_trust_inventory(package)

    assert inventory.kind == "bounded_no_symlink"
    assert inventory.paths == ("visible.py",)
    assert inventory.rejected == (type(inventory.rejected[0])("hidden.py", "symlink_escape"),)
    assert inventory.targeted_freshness(["visible.py"]) == ()
    before = inventory.targeted_states(["visible.py"])
    assert len(before) == 1
    assert before[0].trusted
    assert before[0].size == len("x = 1\n")
    assert before[0].mtime_ns is not None
    assert before[0].inode is not None
    assert before[0].ctime_ns is not None
    (package / "visible.py").unlink()
    assert inventory.targeted_freshness(["visible.py"]) == (type(inventory.rejected[0])("visible.py", "missing"),)
    assert inventory.targeted_states(["visible.py"])[0].reason == "missing"

    package_link = tmp_path / "transformers-link"
    package_link.symlink_to(package, target_is_directory=True)
    with pytest.raises(ValueError, match="non-symlink directory"):
        transformers_trust_inventory(package_link)


def test_supported_extensions_are_the_fixed_python_and_javascript_typescript_set() -> None:
    expected = {".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
    assert expected == SUPPORTED_EXTENSIONS


def test_content_identity_reports_an_atomic_replacement_that_keeps_size_and_mtime(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    target = root / "module.py"
    target.write_text("x = 1\n")
    inventory = git_trust_inventory(root)
    before = inventory.targeted_states(["module.py"])[0]

    replacement = root / "module.py.tmp"
    replacement.write_text("x = 2\n")
    assert before.mtime_ns is not None
    os.utime(replacement, ns=(before.mtime_ns, before.mtime_ns))
    os.replace(replacement, target)
    after = inventory.targeted_states(["module.py"])[0]

    # Size and mtime are deliberately identical; only the inode reports the swap.
    assert (after.size, after.mtime_ns) == (before.size, before.mtime_ns)
    assert after.inode != before.inode
    assert after.content_identity != before.content_identity
