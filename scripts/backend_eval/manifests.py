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
That includes the trust inventory: rather than calling
``serena_light.workspace.inventory.git_trust_inventory``, which would start its own
unbounded ``git ls-files``, this module reads the *same* combined
``git ls-files --cached --others --exclude-standard -z`` through the bounded runner and
hands those bytes to production's own pure candidate normalization and inspection helpers.
Decoding, normalization, extension filtering, guarded candidate inspection, rejection
reasons, the path digest, and the query tree are therefore production's code, unchanged, and
the resulting inventory is identical to the one production would have built --
:func:`_git_trust_inventory_from_bounded_bytes` and its equivalence test own that claim.
No evaluation code path calls ``git_trust_inventory``.

**Fail-closed individual freezes.**  One capture re-reads every Git-derived control after
its records exist; a revision, inventory, or tracked/untracked change observed while
freezing is a race, and the capture fails rather than returning a manifest describing two
different filesystem states.
"""

from __future__ import annotations

import os
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from scripts.backend_eval.models import PathRecord, RootManifest, canonical_json, sha256_bytes
from scripts.backend_eval.process import CommandTimeout, Deadline, run_bounded_bytes
from serena_light.workspace.identity import open_guarded_directory

# Evaluation-only reuse of production's *pure* inventory helpers.  These are executed
# production semantics, so :mod:`scripts.backend_eval.source_binding` resolves them from this
# evaluator's own checkout and binds their bytes into every receipt.
#  Neither touches a
# subprocess: they only decode, normalize, and inspect candidate paths through guarded
# descriptors.  Importing them is what lets the evaluation bound the one Git command
# ``git_trust_inventory`` would otherwise start for itself while keeping byte-identical
# inventory semantics; `_git_trust_inventory_from_bounded_bytes` is tested against
# ``git_trust_inventory`` for exact equality of root, kind, paths, count, digest, and
# rejections.
from serena_light.workspace.inventory import (
    TrustInventory,
    _decode_git_path,
    _inventory_from_candidates,
    bounded_non_git_trust_inventory,
    observe_file_digest,
)

SERENA_LIGHT_ROOT = Path("/data/CoordExp/serena-light")
MS_SWIFT_ROOT = Path("/data/ms-swift")
RESEARCH_PROBES_ROOT = Path("/data/CoordExp/.worktrees/research-probes")
MS_TRANSFORMERS_ROOT = Path("/root/miniconda3/envs/ms/lib/python3.12/site-packages/transformers")
LLM_FRAMEWORK_STUDY_SITE_PACKAGES = Path("/root/miniconda3/envs/llm-framework-study/lib/python3.12/site-packages")

# The only trees a Git corpus scan prunes.  Each is service- or repository-owned, is not
# part of the evaluated corpus, and every pruned path is published in the manifest.
EXCLUDED_DIRECTORY_NAMES: tuple[str, ...] = (".admission-artifacts", ".git", ".venv", "node_modules")

_MAX_SCAN_DEPTH = 128
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
# Git receives an explicit environment: no ambient GIT_* control may steer the corpus scan.
_GIT_ENVIRONMENT_PATH = "/usr/bin:/bin"
_GIT_EXECUTABLE = shutil.which("git", path=_GIT_ENVIRONMENT_PATH) or shutil.which("git") or "/usr/bin/git"

Check = Callable[[], None]


class ManifestError(RuntimeError):
    """The requested root cannot be frozen without weakening its boundary."""


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
    *, check: Check | None = None, deadline: Deadline | None = None
) -> tuple[RootManifest, ...]:
    """Freeze every fixed corpus root in canonical root order."""

    return tuple(
        capture_root_manifest(request, check=check, deadline=deadline) for request in default_corpus_requests()
    )


def capture_root_manifest(
    request: RootManifestRequest, *, check: Check | None = None, deadline: Deadline | None = None
) -> RootManifest:
    """Capture one stable bounded root or fail before returning partial evidence."""

    root = request.root
    _require_directory(root, "root")
    stop = _checker(check, deadline)
    if request.kind == "git":
        return _capture_git_manifest(root, request, stop, deadline)
    return _capture_non_git_manifest(root, request, stop, deadline)


def _capture_git_manifest(
    root: Path, request: RootManifestRequest, stop: Check, deadline: Deadline | None
) -> RootManifest:
    _require_git_root(root, deadline)
    before, inventory = _git_freeze_state(root, deadline)
    _reject_inventory_rejections(inventory)
    hashed_names = set(inventory.paths) | set(request.fully_hashed_paths) | set(request.required_config_paths)
    disposition_for = _git_disposition_reader(before)
    records = tuple(_hashed_record(root, relative, disposition_for(relative)) for relative in sorted(hashed_names))
    remainder, excluded = _scan_remainder(root, hashed_names, disposition_for, stop)
    _require_disjoint_records(records, remainder)
    after, _ = _git_freeze_state(root, deadline)
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
    root: Path, request: RootManifestRequest, stop: Check, deadline: Deadline | None
) -> RootManifest:
    del deadline
    if root == MS_TRANSFORMERS_ROOT:
        return _capture_transformers_manifest(root, request, stop)
    if not request.fully_hashed_paths and not request.required_config_paths:
        raise ManifestError("non-Git roots require an exact fully hashed task path list")
    hashed_paths = tuple(sorted(set(request.fully_hashed_paths) | set(request.required_config_paths)))
    records = tuple(_hashed_record(root, relative, "declared") for relative in hashed_paths)
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


def _capture_transformers_manifest(root: Path, request: RootManifestRequest, stop: Check) -> RootManifest:
    try:
        inventory = bounded_non_git_trust_inventory(root)
    except (OSError, ValueError) as error:
        raise ManifestError(f"cannot capture bounded transformers inventory: {error}") from error
    _reject_inventory_rejections(inventory)
    hashed_paths = set(inventory.paths) | set(request.fully_hashed_paths) | set(request.required_config_paths)
    records = tuple(_hashed_record(root, relative, "declared") for relative in sorted(hashed_paths))
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
    try:
        root_fd = os.open(root, _DIRECTORY_FLAGS)
    except OSError as error:
        raise ManifestError(f"cannot open corpus root {root}: {error}") from error
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
    """An explicit Git environment: no ambient ``GIT_*`` may steer the corpus scan."""

    env = {"PATH": _GIT_ENVIRONMENT_PATH, "LANG": "C.UTF-8"}
    home = os.environ.get("HOME")
    if home:
        # HOME is kept because the user's global excludes file defines untracked disposition.
        env["HOME"] = home
    return env


def _git_bytes(root: Path, args: tuple[str, ...], deadline: Deadline | None) -> bytes:
    timeout = None if deadline is None else deadline.remaining()
    try:
        result = run_bounded_bytes(
            [_GIT_EXECUTABLE, *args], cwd=root, env=_git_environment(), timeout=timeout
        )
    except CommandTimeout as error:
        raise ManifestError(f"Git command timed out for {root}: {' '.join(args)}: {error}") from error
    except OSError as error:
        raise ManifestError(f"Git command failed for {root}: {error}") from error
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


def _git_trust_inventory_from_bounded_bytes(root: Path, payload: bytes) -> TrustInventory:
    """Build production's Git trust inventory from bytes this module already bounded.

    ``git_trust_inventory`` differs from this function in exactly one respect: it starts its
    own unbounded ``git ls-files --cached --others --exclude-standard -z``, while the
    evaluation reads that same command through :func:`run_bounded_bytes` and passes the
    bytes here.  The root is resolved the same way, the bytes are split and decoded by
    production's ``_decode_git_path``, and the candidates go through production's
    ``_inventory_from_candidates``, so the accepted paths, their digest, the rejected
    entries and reasons, the query tree, and the recorded kind are all production's.
    """

    try:
        resolved_root = root.resolve(strict=True)
    except OSError as error:
        raise ManifestError(f"cannot resolve the Git corpus root {root}: {error}") from error
    candidates = (_decode_git_path(item) for item in payload.split(b"\0") if item)
    try:
        return _inventory_from_candidates(resolved_root, candidates, kind="git")
    except (OSError, ValueError) as error:
        raise ManifestError(f"cannot capture Git trust inventory: {error}") from error


def _git_freeze_state(root: Path, deadline: Deadline | None) -> tuple[_GitFreezeState, TrustInventory]:
    """Capture every Git-derived closure fact used by one manifest pass.

    Four bounded Git children, and no others: the revision, the tracked set, the untracked
    set, and the combined candidate list the trust inventory is built from.
    """

    source_revision = _git_stdout(root, ("rev-parse", "HEAD"), deadline)
    tracked = _git_path_set(root, ("ls-files", "--cached", "-z"), deadline)
    untracked = _git_path_set(root, ("ls-files", "--others", "--exclude-standard", "-z"), deadline)
    combined = _git_bytes(root, ("ls-files", "--cached", "--others", "--exclude-standard", "-z"), deadline)
    inventory = _git_trust_inventory_from_bounded_bytes(root, combined)
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
