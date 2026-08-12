"""Bounded, lexical manifests for the Python-backend evaluation corpus.

The evaluation must record source inputs without taking ownership of mutable
worktrees.  Git source closure and the one large external package deliberately
reuse Serena Light's production trust inventories; the remaining external
environment is restricted to an explicit task-path list.

**A Git root is scanned completely.**  The trust-inventory closure and every declared
configuration path are fully content hashed.  Everything else below the root -- ordinary
files, symlinks with their targets, *directories including empty ones*, and any special
node -- is captured metadata-only by path, type, symlink target, size, ``mtime_ns``, and
inode.  Only four service- or repository-owned trees are pruned, by name, to keep the scan
bounded: ``.git``, the evaluation ``.admission-artifacts``, a lane-owned ``.venv``, and
``node_modules``.  Every pruned path is recorded in the manifest, so the exclusion is
evidence rather than a silent hole.  ``research-probes/model_cache`` is deliberately in
scope.

**Every Git child is bounded, with no exception.**  Every Git invocation runs through the
shared bounded runner with an explicit remaining-time budget, an explicit minimal
environment with no ambient ``GIT_*`` control, and process-group termination on expiry.
``HOME`` is absent, system config is disabled, and the only protected global config is a
sealed one-entry image naming the exact declared root as ``safe.directory``; the same exact
root is also present as a ``-c`` argument.  Ambient system and user credentials, identity,
includes, and global exclude files therefore cannot steer the scan.  Repository-local ignore
semantics such as ``.gitignore`` and ``.git/info/exclude`` remain active.
That includes the trust inventory: rather than calling
``serena_light.workspace.inventory.git_trust_inventory``, which would start its own
unbounded ``git ls-files``, this module reads the *same* combined
``git ls-files --cached --others --exclude-standard -z`` through the bounded runner and
hands those bytes to production's own pure candidate normalization and inspection helpers.
Decoding, normalization, extension filtering, guarded candidate inspection, rejection
reasons, and the path digest are therefore production's code, unchanged, and the resulting
inventory is identical to the one production would have built --
:func:`_git_trust_inventory_from_bounded_bytes` and its equivalence test own that claim.
No evaluation code path calls ``git_trust_inventory``.

**This module imports no production code.**  It used to import
``bounded_non_git_trust_inventory``, ``_decode_git_path``, ``_inventory_from_candidates``,
and ``open_guarded_directory`` into the *evaluator* process.  Python compiled whatever bytes
were on disk at import time, and :func:`~scripts.backend_eval.identity.capture_evaluator_identity`
re-read those same paths afterwards -- so bytes swapped between the import and the capture
would have published a receipt naming one closure while the corpus evidence in it was computed
by another.  Both inventory helpers now execute in the source-bound sealed child, under the
same expectation as every other production helper, and only the evidence a
:class:`~scripts.backend_eval.models.RootManifest` is built from crosses back as canonical
JSON.  The directory traversal is evaluator-owned code here rather than production's opener:
it is a walk, not a semantic the receipt binds.  A regression proves in a fresh interpreter
that importing this module leaves no ``serena_light`` module in the evaluator process.

**Fail-closed individual freezes.**  One capture re-reads every Git-derived control after
its records exist; a revision, inventory, or tracked/untracked change observed while
freezing is a race, and the capture fails rather than returning a manifest describing two
different filesystem states.
"""

from __future__ import annotations

import base64
import os
import re
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from scripts.backend_eval.models import PathRecord, RootManifest, canonical_json, sha256_bytes
from scripts.backend_eval.process import (
    GIT_EXECUTABLE,
    CommandTimeout,
    Deadline,
    ExecutableBindingError,
    SealedImageError,
    bound_executable,
    descriptor_path,
    run_bounded_bytes,
    sealed_image,
)
from scripts.backend_eval.production_helper import ProductionHelperError, run_production_helper
from scripts.backend_eval.source_binding import HelperExpectation, SourceBindingError

SERENA_LIGHT_ROOT = Path("/data/CoordExp/serena-light")
MS_SWIFT_ROOT = Path("/data/ms-swift")
RESEARCH_PROBES_ROOT = Path("/data/CoordExp/.worktrees/research-probes")
MS_TRANSFORMERS_ROOT = Path("/root/miniconda3/envs/ms/lib/python3.12/site-packages/transformers")
LLM_FRAMEWORK_STUDY_SITE_PACKAGES = Path("/root/miniconda3/envs/llm-framework-study/lib/python3.12/site-packages")

# The only trees a Git corpus scan prunes.  Each is service- or repository-owned, is not
# part of the evaluated corpus, and every pruned path is published in the manifest.
EXCLUDED_DIRECTORY_NAMES: tuple[str, ...] = (".admission-artifacts", ".git", ".venv", "node_modules")

# Content digests come from ``observe_file_digest`` executed in a bounded child, in chunks.
# The chunk is a deliberate trade: production's guarded read inspects a candidate's type and
# then reopens it by name with no ``O_NONBLOCK``, so a node substituted in that window blocks
# the reader, and only a killable process group can bound it.  Chunking keeps each child's
# argument list and its own blast radius small; the stability proof is the *whole-pass*
# ``lstat`` bracket in ``_hashed_records``, which is stricter than a per-chunk one because it
# refuses a path that moved at any point during the pass, not merely during its own chunk.
DIGEST_CHUNK_SIZE = 512

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
# The exact inventory kind each delegated operation must report back.
_INVENTORY_KINDS = {"git_inventory_from_bytes": "git", "bounded_non_git_inventory": "bounded_no_symlink"}
_MAX_SCAN_DEPTH = 128
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_SOURCE_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_SOURCE_READ_CHUNK = 64 * 1024
MAX_SOURCE_BYTES = 4 * 1024 * 1024
_MAX_SOURCE_COMPONENTS = 128
# Git receives an explicit environment: no ambient GIT_* control may steer the corpus scan.
_GIT_ENVIRONMENT_PATH = "/usr/bin:/bin"

Check = Callable[[], None]


class ManifestError(RuntimeError):
    """The requested root cannot be frozen without weakening its boundary."""


def read_stable_source_text(
    workspace_root: Path,
    target: Path,
    *,
    deadline: Deadline,
    max_bytes: int = MAX_SOURCE_BYTES,
) -> str:
    """Read one UTF-8 source file through a stable, component-wise no-follow walk."""

    if not workspace_root.is_absolute():
        raise ManifestError("the source workspace root must be absolute")
    source = target if target.is_absolute() else workspace_root / target
    try:
        relative = source.relative_to(workspace_root)
    except ValueError as exc:
        raise ManifestError(f"source target must be lexically inside {workspace_root}: {source}") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ManifestError(f"source target contains traversal outside {workspace_root}: {source}")
    if len(relative.parts) > _MAX_SOURCE_COMPONENTS:
        raise ManifestError(
            f"source target exceeds the maximum component depth of {_MAX_SOURCE_COMPONENTS}: {source}"
        )
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")

    parent_fd, root_fd = _open_stable_source_root(workspace_root, deadline)
    directory_fds = [root_fd]
    directory_bindings: list[tuple[int, str, int, os.stat_result]] = []
    leaf_fd: int | None = None
    try:
        _require_stable_source_root(parent_fd, root_fd, workspace_root, deadline)
        for component in relative.parts[:-1]:
            deadline.check("open source directory")
            try:
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=directory_fds[-1])
            except OSError as exc:
                raise ManifestError(
                    f"cannot open source directory component {component!r} without following a link: {exc}"
                ) from exc
            try:
                opened = os.fstat(child)
                entry = os.stat(component, dir_fd=directory_fds[-1], follow_symlinks=False)
                if (
                    not stat.S_ISDIR(opened.st_mode)
                    or not stat.S_ISDIR(entry.st_mode)
                    or (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino)
                ):
                    raise ManifestError(
                        f"source directory component {component!r} changed before it could be read"
                    )
            except BaseException:
                os.close(child)
                raise
            directory_bindings.append((directory_fds[-1], component, child, opened))
            directory_fds.append(child)
        leaf_parent = directory_fds[-1]
        deadline.check("open source file")
        try:
            leaf_fd = os.open(relative.parts[-1], _SOURCE_READ_FLAGS, dir_fd=leaf_parent)
        except OSError as exc:
            raise ManifestError(f"cannot open source file {source} without following a link: {exc}") from exc
        before = os.fstat(leaf_fd)
        entry_before = os.stat(relative.parts[-1], dir_fd=leaf_parent, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or not stat.S_ISREG(entry_before.st_mode):
            raise ManifestError(f"source target must be a regular file: {source}")
        if (before.st_dev, before.st_ino) != (entry_before.st_dev, entry_before.st_ino):
            raise ManifestError(f"source target changed before it could be read: {source}")
        if before.st_size > max_bytes:
            raise ManifestError(f"source target exceeds the maximum of {max_bytes} bytes: {source}")

        chunks: list[bytes] = []
        size = 0
        while True:
            deadline.check("read source file")
            try:
                chunk = os.read(leaf_fd, min(_SOURCE_READ_CHUNK, max_bytes + 1 - size))
            except OSError as exc:
                raise ManifestError(f"cannot read source file {source}: {exc}") from exc
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > max_bytes:
                raise ManifestError(f"source target exceeds the maximum of {max_bytes} bytes: {source}")

        deadline.check("verify source file")
        after = os.fstat(leaf_fd)
        entry_after = os.stat(relative.parts[-1], dir_fd=leaf_parent, follow_symlinks=False)
        stable_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, name) != getattr(after, name) for name in stable_fields) or (
            after.st_dev,
            after.st_ino,
        ) != (entry_after.st_dev, entry_after.st_ino):
            raise ManifestError(f"source target changed while it was read: {source}")
        _require_stable_source_directories(directory_bindings, source, deadline)
        _require_stable_source_root(parent_fd, root_fd, workspace_root, deadline)
        try:
            return b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ManifestError(f"source target is not valid UTF-8: {source}") from exc
    finally:
        if leaf_fd is not None:
            os.close(leaf_fd)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)
        os.close(parent_fd)


def _open_stable_source_root(root: Path, deadline: Deadline) -> tuple[int, int]:
    """Open the root and retain its parent so later pathname replacement is detectable."""

    if root == Path("/"):
        raise ManifestError("the filesystem root cannot be a source workspace")
    deadline.check("open source workspace root")
    current = _open_source_filesystem_root()
    try:
        for component in root.parts[1:-1]:
            deadline.check("open source workspace component")
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        deadline.check("open source workspace root")
        root_fd = os.open(root.name, _DIRECTORY_FLAGS, dir_fd=current)
    except BaseException as exc:
        os.close(current)
        if isinstance(exc, OSError):
            raise ManifestError(
                f"cannot open source workspace {root} without following a link: {exc}"
            ) from exc
        raise
    return current, root_fd


def _open_source_filesystem_root() -> int:
    """The one guarded anchor for the source reader's confined absolute walk."""

    try:
        return os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise ManifestError(f"cannot open filesystem root for source read: {exc}") from exc


def _require_stable_source_root(parent_fd: int, root_fd: int, root: Path, deadline: Deadline) -> None:
    deadline.check("verify source workspace root")
    try:
        entry = os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
        opened = os.fstat(root_fd)
        physical = Path(os.readlink(f"/proc/self/fd/{root_fd}"))
    except OSError as exc:
        raise ManifestError(f"source workspace root changed while reading {root}: {exc}") from exc
    if (
        not stat.S_ISDIR(entry.st_mode)
        or (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino)
        or physical != root
    ):
        raise ManifestError(f"source workspace root changed while reading {root}")


def _require_stable_source_directories(
    bindings: list[tuple[int, str, int, os.stat_result]], source: Path, deadline: Deadline
) -> None:
    """Re-prove every retained lexical directory edge before source bytes are returned."""

    stable_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    for parent_fd, component, child_fd, before in bindings:
        deadline.check("verify source directory")
        try:
            entry = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            opened = os.fstat(child_fd)
        except OSError as exc:
            raise ManifestError(
                f"source directory component {component!r} changed while reading {source}: {exc}"
            ) from exc
        if (
            not stat.S_ISDIR(entry.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino)
            or any(getattr(before, name) != getattr(opened, name) for name in stable_fields)
        ):
            raise ManifestError(
                f"source directory component {component!r} changed while reading {source}"
            )


@dataclass(frozen=True, slots=True)
class BoundedTrustInventory:
    """The trust-inventory evidence one bounded child computed with production's own bytes.

    This is deliberately *not* production's ``TrustInventory``.  Importing that class would
    put production code back in the evaluator process, which is exactly the exposure this
    module closed: an import compiles whatever is on disk at import time, before the identity
    that names it is captured.  Only the fields a
    :class:`~scripts.backend_eval.models.RootManifest` is built from cross the boundary, and
    they cross as canonical JSON validated on arrival.  The accepted paths, the count, the
    digest, and every rejection reason are production's; nothing here recomputes them.
    """

    root: str
    kind: str
    digest: str
    paths: tuple[str, ...]
    rejected: tuple[tuple[str, str], ...]

    @property
    def count(self) -> int:
        return len(self.paths)


def _noop_check() -> None:
    return None


def _checker(check: Check | None, deadline: Deadline | None) -> Check:
    """One cooperative stop signal for long traversals: the caller's, the deadline's, or none."""

    if check is not None:
        return check
    if deadline is not None:
        return lambda: deadline.check("capture_corpus")
    return _noop_check


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
        if self.kind == "git" and self.metadata_roots:
            raise ManifestError(
                "RootManifestRequest.metadata_roots must be empty for a Git root: the complete in-scope "
                "remainder is scanned"
            )
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
            metadata_roots=(),
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


def freeze_default_corpus(
    *,
    expectation: HelperExpectation,
    check: Check | None = None,
    deadline: Deadline | None = None,
) -> tuple[RootManifest, ...]:
    """Freeze every fixed corpus root in canonical root order."""

    return tuple(
        capture_root_manifest(request, expectation=expectation, check=check, deadline=deadline)
        for request in default_corpus_requests()
    )


def capture_root_manifest(
    request: RootManifestRequest,
    *,
    expectation: HelperExpectation,
    check: Check | None = None,
    deadline: Deadline | None = None,
) -> RootManifest:
    """Capture one stable bounded root or fail before returning partial evidence.

    ``expectation`` is the run's own execution expectation: every content digest below is
    computed by production's bytes in a bounded child, and that child may execute only the
    bytes the captured evaluator identity names.
    """

    root = request.root
    _require_directory(root, "root")
    stop = _checker(check, deadline)
    if request.kind == "git":
        return _capture_git_manifest(root, request, stop, expectation, deadline)
    return _capture_non_git_manifest(root, request, stop, expectation, deadline)


def _capture_git_manifest(
    root: Path,
    request: RootManifestRequest,
    stop: Check,
    expectation: HelperExpectation,
    deadline: Deadline | None,
) -> RootManifest:
    _require_git_root(root, deadline)
    before, inventory = _git_freeze_state(root, expectation, deadline)
    _reject_inventory_rejections(inventory)
    hashed_names = set(inventory.paths) | set(request.fully_hashed_paths) | set(request.required_config_paths)
    disposition_for = _git_disposition_reader(before)
    records = _hashed_records(root, sorted(hashed_names), disposition_for, expectation, deadline)
    remainder, excluded = _scan_remainder(root, hashed_names, disposition_for, stop)
    _require_disjoint_records(records, remainder)
    after, _ = _git_freeze_state(root, expectation, deadline)
    if after != before:
        raise ManifestError("Git manifest inputs changed while freezing")
    return RootManifest.build(
        root=str(root),
        kind="git",
        source_revision=before.source_revision,
        inventory_digest=inventory.digest,
        inventory_paths=tuple(sorted(inventory.paths)),
        excluded_paths=excluded,
        hashed_paths=records,
        metadata_paths=remainder,
    )


def _capture_non_git_manifest(
    root: Path,
    request: RootManifestRequest,
    stop: Check,
    expectation: HelperExpectation,
    deadline: Deadline | None,
) -> RootManifest:
    if root == MS_TRANSFORMERS_ROOT:
        return _capture_transformers_manifest(root, request, stop, expectation, deadline)
    if not request.fully_hashed_paths and not request.required_config_paths:
        raise ManifestError("non-Git roots require an exact fully hashed task path list")
    hashed_paths = tuple(sorted(set(request.fully_hashed_paths) | set(request.required_config_paths)))
    records = _hashed_records(root, hashed_paths, lambda _relative: "declared", expectation, deadline)
    metadata = _metadata_records(root, request.metadata_roots, lambda _relative: "declared", stop)
    _require_disjoint_records(records, metadata)
    inventory_digest = sha256_bytes(canonical_json({"kind": "non_git", "paths": list(hashed_paths)}))
    return RootManifest.build(
        root=str(root),
        kind="non_git",
        source_revision=None,
        inventory_digest=inventory_digest,
        inventory_paths=hashed_paths,
        excluded_paths=(),
        hashed_paths=records,
        metadata_paths=metadata,
    )


def _capture_transformers_manifest(
    root: Path,
    request: RootManifestRequest,
    stop: Check,
    expectation: HelperExpectation,
    deadline: Deadline | None,
) -> RootManifest:
    inventory = _bounded_non_git_inventory(root, expectation, deadline)
    _reject_inventory_rejections(inventory)
    hashed_paths = set(inventory.paths) | set(request.fully_hashed_paths) | set(request.required_config_paths)
    records = _hashed_records(root, sorted(hashed_paths), lambda _relative: "declared", expectation, deadline)
    metadata = _metadata_records(root, request.metadata_roots, lambda _relative: "declared", stop)
    _require_disjoint_records(records, metadata)
    return RootManifest.build(
        root=str(root),
        kind="non_git",
        source_revision=None,
        inventory_digest=inventory.digest,
        inventory_paths=tuple(sorted(inventory.paths)),
        excluded_paths=(),
        hashed_paths=records,
        metadata_paths=metadata,
    )


# --- the complete in-scope remainder ---------------------------------------------


def _scan_remainder(
    root: Path,
    hashed_names: set[str],
    disposition_for: Callable[[str], str],
    stop: Check,
) -> tuple[tuple[PathRecord, ...], tuple[str, ...]]:
    """Metadata-scan everything below ``root`` except the four declared pruned trees.

    Every directory is opened from its parent's descriptor with ``O_NOFOLLOW``, so no
    symlink can redirect the walk out of the root.  Open descriptors are bounded by the
    tree depth, and the depth itself is bounded.
    """

    records: list[PathRecord] = []
    excluded: list[str] = []
    root_fd = _open_declared_corpus_root(root)
    try:
        _walk_remainder(root_fd, "", root, hashed_names, disposition_for, stop, records, excluded, 0)
    finally:
        os.close(root_fd)
    ordered = tuple(sorted(records, key=lambda record: record.path))
    if len({record.path for record in ordered}) != len(ordered):
        raise ManifestError(f"corpus remainder contains duplicate paths below {root}")
    return ordered, tuple(sorted(excluded))


def _walk_remainder(
    dir_fd: int,
    prefix: str,
    root: Path,
    hashed_names: set[str],
    disposition_for: Callable[[str], str],
    stop: Check,
    records: list[PathRecord],
    excluded: list[str],
    depth: int,
) -> None:
    if depth > _MAX_SCAN_DEPTH:
        raise ManifestError(f"corpus remainder below {root} is deeper than {_MAX_SCAN_DEPTH} levels: {prefix}")
    try:
        with os.scandir(dir_fd) as scan:
            names = sorted(entry.name for entry in scan)
    except OSError as error:
        raise ManifestError(f"cannot scan corpus path {root / prefix}: {error}") from error
    for name in names:
        stop()
        relative = f"{prefix}{name}"
        if name in EXCLUDED_DIRECTORY_NAMES:
            excluded.append(relative)
            continue
        try:
            observed = os.lstat(name, dir_fd=dir_fd)
        except FileNotFoundError as error:
            raise ManifestError(f"corpus path vanished while scanning: {relative}") from error
        except OSError as error:
            raise ManifestError(f"cannot lstat corpus path {relative}: {error}") from error
        if stat.S_ISDIR(observed.st_mode):
            records.append(_metadata_record(relative, "directory", observed, disposition_for, None))
            try:
                child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=dir_fd)
            except OSError as error:
                raise ManifestError(f"cannot open corpus directory {relative}: {error}") from error
            try:
                _walk_remainder(
                    child_fd,
                    f"{relative}/",
                    root,
                    hashed_names,
                    disposition_for,
                    stop,
                    records,
                    excluded,
                    depth + 1,
                )
            finally:
                os.close(child_fd)
            continue
        if relative in hashed_names:
            # Already captured with a full content digest; never recorded twice.
            continue
        if stat.S_ISLNK(observed.st_mode):
            try:
                target = os.readlink(name, dir_fd=dir_fd)
            except OSError as error:
                raise ManifestError(f"cannot read corpus symlink {relative}: {error}") from error
            records.append(_metadata_record(relative, "symlink", observed, disposition_for, target))
            continue
        kind = "file" if stat.S_ISREG(observed.st_mode) else "special"
        records.append(_metadata_record(relative, kind, observed, disposition_for, None))


def _metadata_record(
    relative: str,
    kind: str,
    observed: os.stat_result,
    disposition_for: Callable[[str], str],
    symlink_target: str | None,
) -> PathRecord:
    return PathRecord(
        path=relative,
        kind=kind,
        disposition=disposition_for(relative),
        size=observed.st_size,
        mtime_ns=observed.st_mtime_ns,
        inode=observed.st_ino,
        symlink_target=symlink_target,
        content_sha256=None,
    )


def _git_disposition_reader(state: _GitFreezeState) -> Callable[[str], str]:
    """Classify any in-scope path from the two Git path sets, without a per-path subprocess."""

    tracked_directories = _ancestor_directories(state.tracked_paths)
    untracked_directories = _ancestor_directories(state.untracked_paths)

    def disposition_for(relative: str) -> str:
        if relative in state.tracked_paths or relative in tracked_directories:
            return "tracked"
        if relative in state.untracked_paths or relative in untracked_directories:
            return "untracked"
        return "ignored"

    return disposition_for


def _ancestor_directories(paths: frozenset[str]) -> frozenset[str]:
    directories: set[str] = set()
    for path in paths:
        parts = path.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            directories.add("/".join(parts[:index]))
    directories.discard("")
    return frozenset(directories)


# --- declared metadata roots (non-Git only) --------------------------------------


def _open_declared_corpus_root(root: Path) -> int:
    """Open one caller-declared corpus root: the guarded open confinement starts from.

    ``O_DIRECTORY`` refuses a non-directory before any type-specific open handler runs.  It
    proves nothing about the components *above* the root, and the ownership table records it
    as ``guarded`` rather than ``confined`` for exactly that reason.
    """

    try:
        return os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as error:
        raise ManifestError(f"cannot open the corpus root {root}: {error}") from error


def _open_metadata_directory(root: Path, parts: tuple[str, ...]) -> int:
    """Open one in-root metadata directory without traversing a symlinked component.

    Evaluator-owned on purpose.  Production's ``open_guarded_directory`` does exactly this,
    but calling it here would mean importing production code into the evaluator process --
    compiled from whatever is on disk at import time, before the identity that names it is
    captured.  A directory walk is not a semantic the receipt binds, so the honest repair is
    to own the walk rather than to delegate it.

    The boundary, stated exactly: the declared corpus root is opened as given, which is a
    *guarded* open -- ``O_DIRECTORY`` refuses a non-directory, and nothing above the root is
    proven, because the root is where confinement starts.  Every component below it is
    opened from its parent's descriptor with ``O_NOFOLLOW``, so a symlinked component fails
    with ``ELOOP`` and a non-directory with ``ENOTDIR`` before anything is read.
    """

    directory_fd = _open_declared_corpus_root(root)
    try:
        for part in parts:
            if part in {"", ".", ".."}:
                raise ManifestError(f"metadata path component is not normalized: {part!r}")
            child_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
    except OSError as error:
        os.close(directory_fd)
        raise ManifestError(
            f"metadata directory is a symlink or unreadable: {'/'.join(parts)}: {error}"
        ) from error
    except BaseException:
        os.close(directory_fd)
        raise
    return directory_fd


def _metadata_records(
    root: Path,
    metadata_roots: tuple[str, ...],
    disposition_for: Callable[[str], str],
    stop: Check,
) -> tuple[PathRecord, ...]:
    records: list[PathRecord] = []
    for relative_root in metadata_roots:
        directory = root / relative_root
        observed = _lstat(directory, relative_root)
        if stat.S_ISLNK(observed.st_mode):
            raise ManifestError(f"metadata root is a symlink: {relative_root}")
        if not stat.S_ISDIR(observed.st_mode):
            raise ManifestError(f"metadata root is not a directory: {relative_root}")
        records.extend(_walk_metadata_root(root, relative_root, disposition_for, stop))
    ordered = tuple(sorted(records, key=lambda record: record.path))
    if len({record.path for record in ordered}) != len(ordered):
        raise ManifestError("metadata roots contain duplicate paths")
    return ordered


def _walk_metadata_root(
    root: Path,
    relative_directory: str,
    disposition_for: Callable[[str], str],
    stop: Check,
) -> list[PathRecord]:
    directory_fd = _open_metadata_directory(root, tuple(relative_directory.split("/")))
    try:
        try:
            with os.scandir(directory_fd) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except OSError as error:
            raise ManifestError(f"cannot scan metadata root {relative_directory}: {error}") from error
        records: list[PathRecord] = []
        for entry in entries:
            stop()
            relative = f"{relative_directory}/{entry.name}"
            try:
                observed = os.lstat(entry.name, dir_fd=directory_fd)
            except OSError as error:
                raise ManifestError(f"cannot lstat metadata path {relative}: {error}") from error
            if stat.S_ISDIR(observed.st_mode):
                records.append(_metadata_record(relative, "directory", observed, disposition_for, None))
                records.extend(_walk_metadata_root(root, relative, disposition_for, stop))
                continue
            if stat.S_ISLNK(observed.st_mode):
                try:
                    target = os.readlink(entry.name, dir_fd=directory_fd)
                except OSError as error:
                    raise ManifestError(f"cannot read metadata symlink {relative}: {error}") from error
                records.append(_metadata_record(relative, "symlink", observed, disposition_for, target))
                continue
            kind = "file" if stat.S_ISREG(observed.st_mode) else "special"
            records.append(_metadata_record(relative, kind, observed, disposition_for, None))
        return records
    finally:
        os.close(directory_fd)


# --- content-hashed records --------------------------------------------------------


def bounded_file_digests(
    root: Path, relatives: Sequence[str], *, expectation: HelperExpectation, deadline: Deadline | None
) -> dict[str, str | None]:
    """Digest every named path with production's ``observe_file_digest``, in bounded children.

    The helper's own semantics are preserved exactly -- it is production's bytes that run --
    and ``None`` still means "no byte identity exists to attribute".  What the child adds is
    a ceiling: a substituted FIFO that blocks production's guarded open costs this phase its
    remaining time and a typed failure, with the whole process group killed, instead of
    hanging the evaluator inside one uninterruptible syscall.
    """

    digests: dict[str, str | None] = {}
    for start in range(0, len(relatives), DIGEST_CHUNK_SIZE):
        chunk = tuple(relatives[start : start + DIGEST_CHUNK_SIZE])
        if deadline is not None:
            deadline.check("capture_corpus")
        digests.update(_digest_chunk(root, chunk, expectation, deadline))
    return digests


def _digest_chunk(
    root: Path, chunk: Sequence[str], expectation: HelperExpectation, deadline: Deadline | None
) -> dict[str, str | None]:
    paths = [str(root / relative) for relative in chunk]
    try:
        result = run_production_helper(
            "observe_file_digests", {"paths": paths}, expectation=expectation, deadline=deadline
        )
    except (ProductionHelperError, SourceBindingError) as error:
        raise ManifestError(f"cannot digest the hashed closure below {root}: {error}") from error
    recorded = result.get("digests")
    if not isinstance(recorded, list) or len(recorded) != len(chunk):
        raise ManifestError(f"the digest helper did not answer every hashed path below {root}")
    digests: dict[str, str | None] = {}
    for relative, expected, entry in zip(chunk, paths, recorded, strict=True):
        if not isinstance(entry, list) or len(entry) != 2 or entry[0] != expected:
            raise ManifestError(f"the digest helper answered a different path than {relative}")
        digest = entry[1]
        if digest is not None and not isinstance(digest, str):
            raise ManifestError(f"the digest helper answered a malformed digest for {relative}")
        digests[relative] = digest
    return digests


def _hashed_records(
    root: Path,
    relatives: Sequence[str],
    disposition_for: Callable[[str], str],
    expectation: HelperExpectation,
    deadline: Deadline | None,
) -> tuple[PathRecord, ...]:
    """Bracket the whole bounded digest pass with an ``lstat`` before and after every path.

    The bracket is deliberately *whole-pass*, not per-chunk: every path is ``lstat``ed before
    the first chunk starts and again after the last one finishes, and any path whose identity
    moved anywhere inside that window fails the capture.  That is a wider window and a
    stricter requirement than a per-chunk bracket -- a path that held still for its own chunk
    but moved during another one is refused here and would be accepted there.
    """

    before = {relative: _require_hashable(root, relative) for relative in relatives}
    digests = bounded_file_digests(root, relatives, expectation=expectation, deadline=deadline)
    records: list[PathRecord] = []
    for relative in relatives:
        digest = digests.get(relative)
        after = _lstat(root / relative, relative)
        if digest is None or _stat_identity(before[relative]) != _stat_identity(after):
            raise ManifestError(f"fully hashed path is unstable: {relative}")
        records.append(
            PathRecord(
                path=relative,
                kind="file",
                disposition=disposition_for(relative),
                size=after.st_size,
                mtime_ns=after.st_mtime_ns,
                inode=after.st_ino,
                symlink_target=None,
                content_sha256=digest,
            )
        )
    return tuple(records)


def _require_hashable(root: Path, relative: str) -> os.stat_result:
    observed = _lstat(root / relative, relative)
    if stat.S_ISLNK(observed.st_mode):
        raise ManifestError(f"fully hashed path is a symlink: {relative}")
    if not stat.S_ISREG(observed.st_mode):
        raise ManifestError(f"fully hashed path is not a regular file: {relative}")
    return observed


# --- Git facts ----------------------------------------------------------------------


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


def _require_git_root(root: Path, deadline: Deadline | None) -> None:
    top_level = Path(_git_stdout(root, ("rev-parse", "--show-toplevel"), deadline))
    if top_level != root:
        raise ManifestError(f"Git root is not exact: {root}")


def _git_environment() -> dict[str, str]:
    """An explicit Git environment with no system, global, user, or credential config."""

    return {
        "GIT_CONFIG_NOSYSTEM": "1",
        "LANG": "C.UTF-8",
        "PATH": _GIT_ENVIRONMENT_PATH,
    }


def _git_safe_directory_config(root: Path) -> bytes:
    """One protected global-scope value, with no includes, credentials, or user settings."""

    value = os.fspath(root)
    if any(character in value for character in ('"', "\\", "\n", "\r", "\0")):
        raise ManifestError(f"Git root cannot be represented safely in the trust config: {root}")
    return f'[safe]\n\tdirectory = "{value}"\n'.encode()


def _git_bytes(root: Path, args: tuple[str, ...], deadline: Deadline | None) -> bytes:
    timeout = None if deadline is None else deadline.remaining()
    try:
        executable = bound_executable(GIT_EXECUTABLE)
    except ExecutableBindingError as error:
        raise ManifestError(f"the declared Git executable cannot be bound for {root}: {error}") from error
    try:
        with sealed_image("backend-eval-git-config", _git_safe_directory_config(root)) as config_fd:
            environment = _git_environment()
            environment["GIT_CONFIG_GLOBAL"] = str(descriptor_path(config_fd))
            result = run_bounded_bytes(
                [str(executable), "-c", f"safe.directory={root}", *args],
                cwd=root,
                env=environment,
                timeout=timeout,
                pass_fds=(config_fd,),
            )
    except CommandTimeout as error:
        raise ManifestError(f"Git command timed out for {root}: {' '.join(args)}: {error}") from error
    except OSError as error:
        raise ManifestError(f"Git command failed for {root}: {error}") from error
    except SealedImageError as error:
        raise ManifestError(f"cannot build the explicit Git trust config for {root}: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ManifestError(f"Git command failed for {root} ({result.returncode}): {' '.join(args)}: {detail}")
    return result.stdout


def _git_stdout(root: Path, args: tuple[str, ...], deadline: Deadline | None) -> str:
    value = _git_bytes(root, args, deadline).decode("utf-8", "surrogateescape").strip()
    if not value:
        raise ManifestError(f"Git command returned no value for {root}: {' '.join(args)}")
    return value


def _git_path_set(root: Path, args: tuple[str, ...], deadline: Deadline | None) -> frozenset[str]:
    payload = _git_bytes(root, args, deadline)
    return frozenset(item.decode("utf-8", "surrogateescape") for item in payload.split(b"\0") if item)


def _git_trust_inventory_from_bounded_bytes(
    root: Path, payload: bytes, expectation: HelperExpectation, deadline: Deadline | None
) -> BoundedTrustInventory:
    """Build production's Git trust inventory from bytes this module already bounded.

    ``git_trust_inventory`` differs from this function in exactly one respect: it starts its
    own unbounded ``git ls-files --cached --others --exclude-standard -z``, while the
    evaluation reads that same command through :func:`run_bounded_bytes` and passes the bytes
    to the child.  There, and only there, the root is resolved the same way, the bytes are
    split and decoded by production's ``_decode_git_path``, and the candidates go through
    production's ``_inventory_from_candidates``, so the accepted paths, their digest, and the
    rejected entries and reasons are all production's -- executed from the sealed image the
    run's own captured identity names.
    """

    return _child_inventory(
        "git_inventory_from_bytes",
        {"root": str(root), "candidates_b64": base64.b64encode(payload).decode("ascii")},
        root,
        expectation,
        deadline,
    )


def _bounded_non_git_inventory(
    root: Path, expectation: HelperExpectation, deadline: Deadline | None
) -> BoundedTrustInventory:
    """Production's bounded non-Git indexing of one exact root, in the sealed child."""

    return _child_inventory(
        "bounded_non_git_inventory", {"root": str(root)}, root, expectation, deadline
    )


def _child_inventory(
    operation: str,
    payload: dict[str, str],
    root: Path,
    expectation: HelperExpectation,
    deadline: Deadline | None,
) -> BoundedTrustInventory:
    """Run one inventory helper in the bounded child and validate the evidence it returns."""

    try:
        result = run_production_helper(operation, payload, expectation=expectation, deadline=deadline)
    except (ProductionHelperError, SourceBindingError) as error:
        raise ManifestError(f"cannot capture the trust inventory below {root}: {error}") from error
    return _parse_inventory_evidence(operation, result, root)


def _parse_inventory_evidence(
    operation: str, result: dict[str, object], root: Path
) -> BoundedTrustInventory:
    """Accept only fully typed inventory evidence; anything else is an incomplete capture."""

    resolved = _expect_text(operation, result, "root", root)
    kind = _expect_text(operation, result, "kind", root)
    digest = _expect_text(operation, result, "digest", root)
    if _SHA256_RE.fullmatch(digest) is None:
        raise ManifestError(f"the {operation} helper returned a malformed digest below {root}")
    if resolved != str(_resolved(root, operation)):
        raise ManifestError(f"the {operation} helper indexed {resolved}, not {root}")
    if kind != _INVENTORY_KINDS[operation]:
        raise ManifestError(f"the {operation} helper reported the kind {kind!r} below {root}")
    raw_paths = result.get("paths")
    if not isinstance(raw_paths, list) or not all(isinstance(item, str) for item in raw_paths):
        raise ManifestError(f"the {operation} helper returned malformed inventory paths below {root}")
    paths = tuple(cast("list[str]", raw_paths))
    if list(paths) != sorted(set(paths)):
        raise ManifestError(f"the {operation} helper returned unsorted or duplicated paths below {root}")
    if _path_digest(paths) != digest:
        raise ManifestError(f"the {operation} helper's digest does not name the paths it returned below {root}")
    raw_rejected = result.get("rejected")
    if not isinstance(raw_rejected, list):
        raise ManifestError(f"the {operation} helper returned malformed rejections below {root}")
    rejected: list[tuple[str, str]] = []
    for entry in cast("list[object]", raw_rejected):
        if not isinstance(entry, list) or len(cast("list[object]", entry)) != 2:
            raise ManifestError(f"the {operation} helper returned a malformed rejection below {root}")
        path, reason = cast("list[object]", entry)
        if not isinstance(path, str) or not isinstance(reason, str):
            raise ManifestError(f"the {operation} helper returned a malformed rejection below {root}")
        rejected.append((path, reason))
    if list(rejected) != sorted(set(rejected)):
        raise ManifestError(f"the {operation} helper returned unsorted or duplicated rejections below {root}")
    return BoundedTrustInventory(
        root=resolved,
        kind=kind,
        digest=digest,
        paths=paths,
        rejected=tuple(rejected),
    )


def _resolved(root: Path, operation: str) -> Path:
    try:
        return root.resolve(strict=True)
    except OSError as error:
        raise ManifestError(f"cannot resolve the corpus root for {operation}: {root}: {error}") from error


def _path_digest(paths: tuple[str, ...]) -> str:
    """Production's own inventory digest formula, recomputed here as an independent check.

    This is not a reimplementation of the inventory: which paths are accepted, and why the
    rest are rejected, is production's answer and is never recomputed.  This one line only
    proves the digest the child returned names exactly the path list it returned alongside it,
    so a malformed or mismatched response is refused rather than published.
    """

    return sha256_bytes("\0".join(paths).encode("utf-8", "surrogateescape"))


def _expect_text(operation: str, result: dict[str, object], name: str, root: Path) -> str:
    value = result.get(name)
    if not isinstance(value, str) or not value:
        raise ManifestError(f"the {operation} helper did not report {name} below {root}")
    return value


def _git_freeze_state(
    root: Path, expectation: HelperExpectation, deadline: Deadline | None
) -> tuple[_GitFreezeState, BoundedTrustInventory]:
    """Capture every Git-derived closure fact used by one manifest pass.

    Four bounded Git children, and no others: the revision, the tracked set, the untracked
    set, and the combined candidate list the trust inventory is built from.
    """

    source_revision = _git_stdout(root, ("rev-parse", "HEAD"), deadline)
    tracked = _git_path_set(root, ("ls-files", "--cached", "-z"), deadline)
    untracked = _git_path_set(root, ("ls-files", "--others", "--exclude-standard", "-z"), deadline)
    combined = _git_bytes(root, ("ls-files", "--cached", "--others", "--exclude-standard", "-z"), deadline)
    inventory = _git_trust_inventory_from_bounded_bytes(root, combined, expectation, deadline)
    return (
        _GitFreezeState(
            source_revision=source_revision,
            inventory_digest=inventory.digest,
            inventory_count=inventory.count,
            inventory_paths=inventory.paths,
            inventory_rejections=inventory.rejected,
            tracked_paths=tracked,
            untracked_paths=untracked,
        ),
        inventory,
    )


def _reject_inventory_rejections(inventory: BoundedTrustInventory) -> None:
    if inventory.rejected:
        path, reason = inventory.rejected[0]
        raise ManifestError(f"trust inventory rejected {path}: {reason}")


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
