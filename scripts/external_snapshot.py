"""Stable identities for explicitly opted-in mutable external acceptance roots."""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import stat
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def snapshot_identity(root: Path) -> str:
    """Return a content-bound identity for a Git root or an allowlisted source tree."""

    resolved = root.resolve(strict=True)
    if _is_git_root(resolved):
        return _git_snapshot_identity(resolved)
    return _source_tree_snapshot_identity(resolved)


def _is_git_root(root: Path) -> bool:
    probe = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        check=False,
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0 and probe.stdout.strip() == "true"


def _git_snapshot_identity(root: Path) -> str:
    head = _git_bytes(root, "rev-parse", "HEAD").decode("ascii").strip()
    tracked_diff = _git_bytes(root, "diff", "--binary", "--full-index", "--no-ext-diff", "HEAD", "--")
    untracked = _git_bytes(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
    digest = hashlib.sha256()
    _update_record(digest, b"schema", b"serena-light-external-git-v1")
    _update_record(digest, b"root", os.fsencode(str(root)))
    _update_record(digest, b"head", head.encode("ascii"))
    _update_record(digest, b"tracked-binary-diff", tracked_diff)
    for relative_bytes in sorted(item for item in untracked if item):
        _update_path_content(digest, root, relative_bytes)
    return f"git:{head}:{digest.hexdigest()}"


def _source_tree_snapshot_identity(root: Path) -> str:
    if root.name != "transformers":
        raise ValueError(f"non-Git external snapshot roots must be the allowlisted transformers package, got {root}")
    version = _distribution_version(root)
    digest = hashlib.sha256()
    _update_record(digest, b"schema", b"serena-light-external-source-tree-v1")
    _update_record(digest, b"root", os.fsencode(str(root)))
    _update_record(digest, b"package", b"transformers")
    _update_record(digest, b"version", version.encode("utf-8"))
    for relative_bytes in _source_tree_paths(root):
        _update_path_content(digest, root, relative_bytes)
    return f"transformers:{version}:{digest.hexdigest()}"


def _distribution_version(root: Path) -> str:
    distributions = importlib.metadata.distributions(path=[str(root.parent)])
    for distribution in distributions:
        metadata_name = distribution.metadata.get("Name", "").lower().replace("_", "-")
        if metadata_name == "transformers":
            return distribution.version
    raise RuntimeError(f"could not find transformers package metadata next to {root}")


def _source_tree_paths(root: Path) -> Iterable[bytes]:
    paths: list[bytes] = []
    for candidate in root.rglob("*"):
        relative = candidate.relative_to(root)
        if "__pycache__" in relative.parts or candidate.suffix == ".pyc":
            continue
        candidate_stat = candidate.lstat()
        if stat.S_ISREG(candidate_stat.st_mode) or stat.S_ISLNK(candidate_stat.st_mode):
            paths.append(os.fsencode(str(relative)))
    return sorted(paths)


def _update_path_content(digest: Any, root: Path, relative_bytes: bytes) -> None:
    relative = Path(os.fsdecode(relative_bytes))
    candidate = root / relative
    candidate_stat = candidate.lstat()
    if stat.S_ISREG(candidate_stat.st_mode):
        _update_record(digest, b"regular", relative_bytes)
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return
    if stat.S_ISLNK(candidate_stat.st_mode):
        _update_record(digest, b"symlink", relative_bytes)
        _update_record(digest, b"target", os.fsencode(os.readlink(candidate)))
        return
    raise RuntimeError(f"snapshot path is no longer a regular file or symlink: {candidate}")


def _update_record(digest: Any, label: bytes, value: bytes) -> None:
    digest.update(len(label).to_bytes(8, "big"))
    digest.update(label)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _git_bytes(root: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Git snapshot command failed for {root}: {' '.join(arguments)}\n{result.stderr.decode(errors='replace')}"
        )
    return result.stdout
