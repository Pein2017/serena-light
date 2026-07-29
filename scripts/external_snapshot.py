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

from serena_light.bootstrap import repository_root, runtime_paths

DEFAULT_SNAPSHOT_PROFILE = "default"
CC_PLUGIN_CODEX_TYPESCRIPT_AUTHORITY_PROFILE = "cc-plugin-codex-typescript-authority-v1"


def snapshot_identity(root: Path, *, profile: str = DEFAULT_SNAPSHOT_PROFILE) -> str:
    """Return a content-bound identity for a Git root or an allowlisted source tree."""

    _validate_profile(profile)
    resolved = root.resolve(strict=True)
    if _is_git_root(resolved):
        return _git_snapshot_identity(resolved, profile=profile)
    if profile != DEFAULT_SNAPSHOT_PROFILE:
        raise ValueError(f"snapshot profile {profile!r} requires a Git root, got {resolved}")
    return _source_tree_snapshot_identity(resolved)


def snapshot_profile_for_environment(environment_name: str) -> str:
    """Return the declared authority profile for an external acceptance environment."""

    if environment_name == "SERENA_LIGHT_CC_PLUGIN_CODEX_SNAPSHOT":
        return CC_PLUGIN_CODEX_TYPESCRIPT_AUTHORITY_PROFILE
    return DEFAULT_SNAPSHOT_PROFILE


def _validate_profile(profile: str) -> None:
    if profile not in {DEFAULT_SNAPSHOT_PROFILE, CC_PLUGIN_CODEX_TYPESCRIPT_AUTHORITY_PROFILE}:
        raise ValueError(f"unknown external snapshot profile: {profile!r}")


def _is_git_root(root: Path) -> bool:
    probe = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        check=False,
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0 and probe.stdout.strip() == "true"


def _git_snapshot_identity(root: Path, *, profile: str) -> str:
    head = _git_bytes(root, "rev-parse", "HEAD").decode("ascii").strip()
    tracked_diff = _git_bytes(root, "diff", "--binary", "--full-index", "--no-ext-diff", "HEAD", "--")
    untracked = _git_bytes(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
    digest = hashlib.sha256()
    _update_record(
        digest,
        b"schema",
        (
            b"serena-light-external-git-v1"
            if profile == DEFAULT_SNAPSHOT_PROFILE
            else b"serena-light-external-git-v2"
        ),
    )
    _update_record(digest, b"root", os.fsencode(str(root)))
    _update_record(digest, b"head", head.encode("ascii"))
    _update_record(digest, b"tracked-binary-diff", tracked_diff)
    for relative_bytes in sorted(item for item in untracked if item):
        _update_path_content(digest, root, relative_bytes)
    for relative_path in _profile_authority_paths(root, profile):
        _update_authority_path_content(digest, root, relative_path)
    return f"git:{head}:{digest.hexdigest()}"


def _profile_authority_paths(root: Path, profile: str) -> tuple[Path, ...]:
    if profile == DEFAULT_SNAPSHOT_PROFILE:
        return ()
    assert profile == CC_PLUGIN_CODEX_TYPESCRIPT_AUTHORITY_PROFILE
    platform_package = f"@typescript/typescript-{_node_platform_architecture(root)}"
    return (
        Path("package.json"),
        Path("package-lock.json"),
        Path("node_modules/.package-lock.json"),
        Path("node_modules/.bin/tsc"),
        Path("node_modules/typescript/package.json"),
        Path("node_modules/typescript/bin/tsc"),
        Path("node_modules/typescript/lib/tsc.js"),
        Path("node_modules/typescript/lib/getExePath.js"),
        Path("node_modules") / platform_package / "package.json",
        Path("node_modules") / platform_package / "lib/tsc",
    )


def _node_platform_architecture(root: Path) -> str:
    """Read the platform from Serena Light's locked Node, never an ambient engine."""

    locked_node = runtime_paths(repository_root())["node"]
    result = subprocess.run(
        [str(locked_node), "--eval", "process.stdout.write(`${process.platform}-${process.arch}`)"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"could not determine Node platform architecture for {root}: {result.stderr.strip()}"
        )
    platform_architecture = result.stdout.strip()
    if not platform_architecture or "/" in platform_architecture or "\\" in platform_architecture:
        raise RuntimeError(f"invalid Node platform architecture: {platform_architecture!r}")
    return platform_architecture


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


def _update_authority_path_content(digest: Any, root: Path, relative_path: Path) -> None:
    """Bind a finite ignored runtime authority path, following launcher symlinks."""

    relative_bytes = os.fsencode(str(relative_path))
    candidate = root / relative_path
    try:
        candidate_stat = candidate.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"required snapshot authority path is missing: {candidate}") from exc
    _update_record(digest, b"authority-path", relative_bytes)
    if stat.S_ISREG(candidate_stat.st_mode):
        _update_path_content(digest, root, relative_bytes)
        return
    if not stat.S_ISLNK(candidate_stat.st_mode):
        raise RuntimeError(f"snapshot authority path is not a regular file or symlink: {candidate}")

    _update_record(digest, b"authority-symlink-target", os.fsencode(os.readlink(candidate)))
    resolved = candidate.resolve(strict=True)
    root_resolved = root.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise RuntimeError(f"snapshot authority symlink escapes external root: {candidate} -> {resolved}")
    resolved_stat = resolved.stat()
    if not stat.S_ISREG(resolved_stat.st_mode):
        raise RuntimeError(f"snapshot authority symlink does not resolve to a regular file: {candidate} -> {resolved}")
    _update_record(digest, b"authority-resolved-path", os.fsencode(str(resolved.relative_to(root_resolved))))
    _update_record(digest, b"authority-resolved-content", resolved.read_bytes())


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
