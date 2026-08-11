"""Bounded, lexical manifests for the Python-backend evaluation corpus.

The evaluation must record source inputs without taking ownership of mutable
worktrees.  Git source closure and the one large external package deliberately
reuse Serena Light's production trust inventories; the remaining external
environment is restricted to an explicit task-path list.
"""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from scripts.backend_eval.models import PathRecord, RootManifest, canonical_json, sha256_bytes
from serena_light.workspace.identity import open_guarded_directory
from serena_light.workspace.inventory import (
    TrustInventory,
    bounded_non_git_trust_inventory,
    git_trust_inventory,
    observe_file_digest,
)

SERENA_LIGHT_ROOT = Path("/data/CoordExp/serena-light")
MS_SWIFT_ROOT = Path("/data/ms-swift")
RESEARCH_PROBES_ROOT = Path("/data/CoordExp/.worktrees/research-probes")
MS_TRANSFORMERS_ROOT = Path("/root/miniconda3/envs/ms/lib/python3.12/site-packages/transformers")
LLM_FRAMEWORK_STUDY_SITE_PACKAGES = Path("/root/miniconda3/envs/llm-framework-study/lib/python3.12/site-packages")


class ManifestError(RuntimeError):
    """The requested root cannot be frozen without weakening its boundary."""


@dataclass(frozen=True, slots=True)
class _GitFreezeState:
    """The Git-backed inputs that must not move during one manifest capture."""

    source_revision: str
    inventory_digest: str
    inventory_count: int
    inventory_paths: tuple[str, ...]
    inventory_rejections: tuple[tuple[str, str], ...]
    tracked_paths: frozenset[str]
    untracked_paths: frozenset[str]


@dataclass(frozen=True, slots=True)
class RootManifestRequest:
    """The explicit bounded inputs required to capture one corpus root."""

    root: Path
    kind: str
    fully_hashed_paths: tuple[str, ...]
    metadata_roots: tuple[str, ...]
    required_config_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path) or not self.root.is_absolute():
            raise ManifestError("RootManifestRequest.root must be an absolute Path")
        if self.kind not in {"git", "non_git"}:
            raise ManifestError("RootManifestRequest.kind must be 'git' or 'non_git'")
        for label, paths in (
            ("fully_hashed_paths", self.fully_hashed_paths),
            ("metadata_roots", self.metadata_roots),
            ("required_config_paths", self.required_config_paths),
        ):
            _validate_request_paths(paths, label)
        declared_hashed = (*self.fully_hashed_paths, *self.required_config_paths)
        if len(set(declared_hashed)) != len(declared_hashed):
            raise ManifestError("fully hashed and required config paths contain duplicate paths")
        _validate_non_overlapping_roots(self.metadata_roots)
        for path in declared_hashed:
            if any(path == root or path.startswith(f"{root}/") for root in self.metadata_roots):
                raise ManifestError("fully hashed paths overlap metadata roots")


def default_corpus_requests() -> tuple[RootManifestRequest, ...]:
    """Return the fixed Phase 1 corpus definition without scanning it."""

    requests = (
        RootManifestRequest(
            root=SERENA_LIGHT_ROOT,
            kind="git",
            fully_hashed_paths=(),
            metadata_roots=(),
            required_config_paths=("pyproject.toml",),
        ),
        RootManifestRequest(
            root=MS_SWIFT_ROOT,
            kind="git",
            fully_hashed_paths=(),
            metadata_roots=(),
            required_config_paths=("setup.cfg",),
        ),
        RootManifestRequest(
            root=RESEARCH_PROBES_ROOT,
            kind="git",
            fully_hashed_paths=(),
            metadata_roots=("model_cache",),
            required_config_paths=("pyrightconfig.json",),
        ),
        RootManifestRequest(
            root=MS_TRANSFORMERS_ROOT,
            kind="non_git",
            fully_hashed_paths=(),
            metadata_roots=(),
            required_config_paths=(),
        ),
        RootManifestRequest(
            root=LLM_FRAMEWORK_STUDY_SITE_PACKAGES,
            kind="non_git",
            fully_hashed_paths=(
                "torchtune/__init__.py",
                "torchtune/config/__init__.py",
                "torchtune/config/_parse.py",
            ),
            metadata_roots=(),
            required_config_paths=(),
        ),
    )
    return tuple(sorted(requests, key=lambda request: str(request.root)))


def freeze_default_corpus() -> tuple[RootManifest, ...]:
    """Freeze every fixed corpus root in canonical root order."""

    return tuple(capture_root_manifest(request) for request in default_corpus_requests())


def capture_root_manifest(request: RootManifestRequest) -> RootManifest:
    """Capture one stable bounded root or fail before returning partial evidence."""

    root = request.root
    _require_directory(root, "root")
    if request.kind == "git":
        return _capture_git_manifest(root, request)
    return _capture_non_git_manifest(root, request)


def _capture_git_manifest(root: Path, request: RootManifestRequest) -> RootManifest:
    _require_git_root(root)
    before, inventory = _git_freeze_state(root)
    _reject_inventory_rejections(inventory)
    source_paths = inventory.paths
    hashed_paths = set(source_paths) | set(request.fully_hashed_paths) | set(request.required_config_paths)
    records = tuple(
        _hashed_record(root, relative, _git_disposition(root, relative, before.tracked_paths, before.untracked_paths))
        for relative in sorted(hashed_paths)
    )
    metadata = _metadata_records(
        root,
        request.metadata_roots,
        lambda relative: _git_disposition(root, relative, before.tracked_paths, before.untracked_paths),
    )
    _require_disjoint_records(records, metadata)
    after, _ = _git_freeze_state(root)
    if after != before:
        raise ManifestError("Git manifest inputs changed while freezing")
    return _root_manifest(
        root=root,
        kind="git",
        source_revision=before.source_revision,
        inventory_digest=inventory.digest,
        inventory_count=inventory.count,
        hashed_paths=records,
        metadata_paths=metadata,
    )


def _capture_non_git_manifest(root: Path, request: RootManifestRequest) -> RootManifest:
    if root == MS_TRANSFORMERS_ROOT:
        return _capture_transformers_manifest(root, request)
    if not request.fully_hashed_paths and not request.required_config_paths:
        raise ManifestError("non-Git roots require an exact fully hashed task path list")
    hashed_paths = tuple(sorted(set(request.fully_hashed_paths) | set(request.required_config_paths)))
    records = tuple(_hashed_record(root, relative, "declared") for relative in hashed_paths)
    metadata = _metadata_records(root, request.metadata_roots, lambda _relative: "declared")
    _require_disjoint_records(records, metadata)
    inventory_digest = sha256_bytes(canonical_json({"kind": "non_git", "paths": list(hashed_paths)}))
    return _root_manifest(
        root=root,
        kind="non_git",
        source_revision=None,
        inventory_digest=inventory_digest,
        inventory_count=len(hashed_paths),
        hashed_paths=records,
        metadata_paths=metadata,
    )


def _capture_transformers_manifest(root: Path, request: RootManifestRequest) -> RootManifest:
    try:
        inventory = bounded_non_git_trust_inventory(root)
    except (OSError, ValueError) as error:
        raise ManifestError(f"cannot capture bounded transformers inventory: {error}") from error
    _reject_inventory_rejections(inventory)
    hashed_paths = set(inventory.paths) | set(request.fully_hashed_paths) | set(request.required_config_paths)
    records = tuple(_hashed_record(root, relative, "declared") for relative in sorted(hashed_paths))
    metadata = _metadata_records(root, request.metadata_roots, lambda _relative: "declared")
    _require_disjoint_records(records, metadata)
    return _root_manifest(
        root=root,
        kind="non_git",
        source_revision=None,
        inventory_digest=inventory.digest,
        inventory_count=inventory.count,
        hashed_paths=records,
        metadata_paths=metadata,
    )


def _root_manifest(
    *,
    root: Path,
    kind: str,
    source_revision: str | None,
    inventory_digest: str,
    inventory_count: int,
    hashed_paths: tuple[PathRecord, ...],
    metadata_paths: tuple[PathRecord, ...],
) -> RootManifest:
    manifest_digest = sha256_bytes(
        canonical_json(
            {
                "root": str(root),
                "kind": kind,
                "source_revision": source_revision,
                "inventory_digest": inventory_digest,
                "inventory_count": inventory_count,
                "hashed_paths": [_record_mapping(record) for record in hashed_paths],
                "metadata_paths": [_record_mapping(record) for record in metadata_paths],
            }
        )
    )
    return RootManifest(
        root=str(root),
        kind=kind,
        source_revision=source_revision,
        inventory_digest=inventory_digest,
        inventory_count=inventory_count,
        hashed_paths=hashed_paths,
        metadata_paths=metadata_paths,
        manifest_digest=manifest_digest,
    )


def _record_mapping(record: PathRecord) -> dict[str, object]:
    return {
        "path": record.path,
        "kind": record.kind,
        "disposition": record.disposition,
        "size": record.size,
        "mtime_ns": record.mtime_ns,
        "inode": record.inode,
        "symlink_target": record.symlink_target,
        "content_sha256": record.content_sha256,
    }


def _hashed_record(root: Path, relative: str, disposition: str) -> PathRecord:
    absolute = root / relative
    before = _lstat(absolute, relative)
    if stat.S_ISLNK(before.st_mode):
        raise ManifestError(f"fully hashed path is a symlink: {relative}")
    if not stat.S_ISREG(before.st_mode):
        raise ManifestError(f"fully hashed path is not a regular file: {relative}")
    digest = observe_file_digest(absolute)
    after = _lstat(absolute, relative)
    if digest is None or _stat_identity(before) != _stat_identity(after):
        raise ManifestError(f"fully hashed path is unstable: {relative}")
    return PathRecord(
        path=relative,
        kind="file",
        disposition=disposition,
        size=after.st_size,
        mtime_ns=after.st_mtime_ns,
        inode=after.st_ino,
        symlink_target=None,
        content_sha256=digest,
    )


def _metadata_records(
    root: Path,
    metadata_roots: tuple[str, ...],
    disposition_for: Callable[[str], str],
) -> tuple[PathRecord, ...]:
    records: list[PathRecord] = []
    for relative_root in metadata_roots:
        directory = root / relative_root
        observed = _lstat(directory, relative_root)
        if stat.S_ISLNK(observed.st_mode):
            raise ManifestError(f"metadata root is a symlink: {relative_root}")
        if not stat.S_ISDIR(observed.st_mode):
            raise ManifestError(f"metadata root is not a directory: {relative_root}")
        records.extend(_walk_metadata_root(root, relative_root, disposition_for))
    ordered = tuple(sorted(records, key=lambda record: record.path))
    if len({record.path for record in ordered}) != len(ordered):
        raise ManifestError("metadata roots contain duplicate paths")
    return ordered


def _walk_metadata_root(
    root: Path,
    relative_directory: str,
    disposition_for: Callable[[str], str],
) -> list[PathRecord]:
    try:
        directory_fd = open_guarded_directory(root, tuple(relative_directory.split("/")))
    except OSError as error:
        raise ManifestError(f"metadata directory is a symlink or unreadable: {relative_directory}: {error}") from error
    try:
        try:
            with os.scandir(directory_fd) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except OSError as error:
            raise ManifestError(f"cannot scan metadata root {relative_directory}: {error}") from error
        records: list[PathRecord] = []
        for entry in entries:
            relative = f"{relative_directory}/{entry.name}"
            try:
                observed = os.lstat(entry.name, dir_fd=directory_fd)
            except OSError as error:
                raise ManifestError(f"cannot lstat metadata path {relative}: {error}") from error
            if stat.S_ISDIR(observed.st_mode):
                records.extend(_walk_metadata_root(root, relative, disposition_for))
                continue
            if stat.S_ISLNK(observed.st_mode):
                try:
                    target = os.readlink(entry.name, dir_fd=directory_fd)
                except OSError as error:
                    raise ManifestError(f"cannot read metadata symlink {relative}: {error}") from error
                records.append(
                    PathRecord(
                        path=relative,
                        kind="symlink",
                        disposition=disposition_for(relative),
                        size=observed.st_size,
                        mtime_ns=observed.st_mtime_ns,
                        inode=observed.st_ino,
                        symlink_target=target,
                        content_sha256=None,
                    )
                )
                continue
            if not stat.S_ISREG(observed.st_mode):
                raise ManifestError(f"metadata path is not a supported regular file or symlink: {relative}")
            records.append(
                PathRecord(
                    path=relative,
                    kind="file",
                    disposition=disposition_for(relative),
                    size=observed.st_size,
                    mtime_ns=observed.st_mtime_ns,
                    inode=observed.st_ino,
                    symlink_target=None,
                    content_sha256=None,
                )
            )
        return records
    finally:
        os.close(directory_fd)


def _require_directory(path: Path, label: str) -> None:
    try:
        observed = path.lstat()
    except FileNotFoundError as error:
        raise ManifestError(f"{label} is missing: {path}") from error
    except OSError as error:
        raise ManifestError(f"cannot inspect {label}: {path}: {error}") from error
    if stat.S_ISLNK(observed.st_mode):
        raise ManifestError(f"{label} is a symlink: {path}")
    if not stat.S_ISDIR(observed.st_mode):
        raise ManifestError(f"{label} is not a directory: {path}")


def _require_git_root(root: Path) -> None:
    top_level = Path(_git_stdout(root, "rev-parse", "--show-toplevel"))
    if top_level != root:
        raise ManifestError(f"Git root is not exact: {root}")


def _git_stdout(root: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ManifestError(f"Git command failed for {root}: {error}") from error
    value = completed.stdout.strip()
    if not value:
        raise ManifestError(f"Git command returned no value for {root}: {' '.join(args)}")
    return value


def _git_path_sets(root: Path) -> tuple[frozenset[str], frozenset[str]]:
    return (
        _git_path_set(root, "ls-files", "--cached", "-z"),
        _git_path_set(root, "ls-files", "--others", "--exclude-standard", "-z"),
    )


def _git_freeze_state(root: Path) -> tuple[_GitFreezeState, TrustInventory]:
    """Capture every Git-derived closure fact used by one manifest pass."""

    source_revision = _git_stdout(root, "rev-parse", "HEAD")
    try:
        inventory = git_trust_inventory(root)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise ManifestError(f"cannot capture Git trust inventory: {error}") from error
    tracked, untracked = _git_path_sets(root)
    return (
        _GitFreezeState(
            source_revision=source_revision,
            inventory_digest=inventory.digest,
            inventory_count=inventory.count,
            inventory_paths=inventory.paths,
            inventory_rejections=tuple((entry.path, entry.reason) for entry in inventory.rejected),
            tracked_paths=tracked,
            untracked_paths=untracked,
        ),
        inventory,
    )


def _git_path_set(root: Path, *args: str) -> frozenset[str]:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ManifestError(f"cannot inspect Git path disposition: {error}") from error
    return frozenset(item.decode("utf-8", "surrogateescape") for item in completed.stdout.split(b"\0") if item)


def _git_disposition(root: Path, relative: str, tracked: frozenset[str], untracked: frozenset[str]) -> str:
    if relative in tracked:
        return "tracked"
    if relative in untracked:
        return "untracked"
    try:
        completed = subprocess.run(
            ["git", "check-ignore", "--quiet", "--", relative],
            cwd=root,
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise ManifestError(f"cannot inspect Git ignored disposition: {error}") from error
    if completed.returncode == 0:
        return "ignored"
    if completed.returncode == 1:
        return "declared"
    raise ManifestError(f"Git ignored-disposition check failed for {relative}")


def _reject_inventory_rejections(inventory: TrustInventory) -> None:
    if inventory.rejected:
        first = inventory.rejected[0]
        raise ManifestError(f"trust inventory rejected {first.path}: {first.reason}")


def _require_disjoint_records(hashed: tuple[PathRecord, ...], metadata: tuple[PathRecord, ...]) -> None:
    overlap = {record.path for record in hashed} & {record.path for record in metadata}
    if overlap:
        raise ManifestError(f"hashed and metadata paths overlap: {sorted(overlap)}")


def _lstat(path: Path, relative: str) -> os.stat_result:
    try:
        return path.lstat()
    except FileNotFoundError as error:
        raise ManifestError(f"path is missing: {relative}") from error
    except OSError as error:
        raise ManifestError(f"cannot lstat path {relative}: {error}") from error


def _stat_identity(observed: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        observed.st_dev,
        observed.st_ino,
        observed.st_mode,
        observed.st_size,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
    )


def _validate_request_paths(paths: object, label: str) -> None:
    if not isinstance(paths, tuple):
        raise ManifestError(f"RootManifestRequest.{label} must be a tuple")
    seen: set[str] = set()
    for raw in paths:
        if not isinstance(raw, str) or not raw:
            raise ManifestError(f"RootManifestRequest.{label} paths must be non-empty strings")
        if raw.startswith("/"):
            raise ManifestError(f"RootManifestRequest.{label} paths must be relative")
        parts = raw.split("/")
        if any(part == ".." for part in parts):
            raise ManifestError(f"RootManifestRequest.{label} paths must not contain traversal")
        if any(part in {"", "."} for part in parts):
            raise ManifestError(f"RootManifestRequest.{label} paths must be normalized")
        if raw in seen:
            raise ManifestError(f"RootManifestRequest.{label} contains duplicate path: {raw}")
        seen.add(raw)


def _validate_non_overlapping_roots(roots: tuple[str, ...]) -> None:
    for index, root in enumerate(roots):
        for other in roots[index + 1 :]:
            if root == other or root.startswith(f"{other}/") or other.startswith(f"{root}/"):
                raise ManifestError("metadata roots overlap")
