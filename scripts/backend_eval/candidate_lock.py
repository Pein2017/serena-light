"""Freeze the ty and pyrefly candidate resolution exactly once, outside production.

The candidate lock is compiled with one explicit ``uv pip compile`` invocation whose
input, output, receipt, and cache live below an evaluation-owned artifact root under
``<repo_root>/.admission-artifacts/backend-eval/``.  The resolution is hash-locked,
binary-only, non-pre-release, and time-bounded by ``--exclude-newer``; the parser
refuses anything the freeze cannot reproduce (missing hashes, editable or direct-URL
requirements, environment markers, duplicates, pre-release or local versions).

Every artifact path component and every artifact file is opened relative to a
directory descriptor with ``O_NOFOLLOW`` and must be a real directory or regular
file, and every write is an atomic same-directory replacement, so no symlink or
special file can redirect a write out of the artifact root.

A resolution runs as a durable, recoverable transaction.  Before the runner starts, a
marker records which canonical artifacts existed and the existing lock and receipt are
*moved* (never copied or deleted) to same-directory ``.rollback`` entries, each backup
file is fsynced through a verified regular-file descriptor, and the directory is fsynced.
The runner-produced lock is opened exactly once, so the bytes that are parsed and the
inode that is fsynced are the same file, and that fsync happens before the receipt is
published; the marker is cleared and the backups are dropped only after the lock file,
the receipt file, production identity, and the directory entries are all durable.

Production identity is re-validated inside the transaction and the new receipt is
published only after that check passes, so a run that drifts production can never leave
a reusable freeze behind.  Any failure quarantines the
canonical nodes -- of any type, including a non-empty directory -- descriptor-relatively
without following links, atomically renames the durable copies back, and fsyncs before
anything is purged, so the only durable copy is never deleted first.  The marker is
cleared only once the intended canonical state is durable, and the next call recovers
any transaction interrupted at any point.

A resolution is also serialized *between callers*.  The whole critical section --
recovery, acceptance, resolution, and publication -- runs under an exclusive ``flock`` on a
per-identity ``O_NOFOLLOW`` lock file inside the artifact directory, so a second caller
never observes another caller's live transaction and mistakes it for an interrupted dead
one.  Recovery therefore only ever sees a marker whose owner has already exited.

The resolver receives :func:`scripts.backend_eval.identity.bootstrap_environment`: the
user's external-network proxy, CA bundle, and locale, plus an exact service-owned
``HOME``/``TMPDIR``/``XDG``/``UV_CACHE_DIR`` below the rebuildable resolver store.  No
ambient ``UV_*``, ``PIP_*``, ``PYTHONPATH``, or ``PATH`` control is inherited, and
``UV_NO_CONFIG`` keeps an ambient ``uv.toml`` or ``pip`` mirror out of the freeze.  Every
resolver invocation is bounded by the caller's remaining time and its process group is
killed on expiry.

Subprocess execution is injected through :class:`CommandRunner`; this module has no
other process seam.
"""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from scripts.backend_eval.identity import bootstrap_environment, bootstrap_service_values
from scripts.backend_eval.models import (
    CandidateLock,
    CandidatePackage,
    LockEvidence,
    ProductionIdentity,
    ResolvedPackage,
    canonical_json,
    sha256_bytes,
)
from scripts.backend_eval.process import (
    CommandResult,
    CommandRunner,
    CommandTimeout,
    Deadline,
    acquire_exclusive_lock,
    subprocess_runner,
)
from scripts.backend_eval.production_identity import (
    ProductionIdentityChanged,
    ProductionIdentityError,
    assert_production_identity_unchanged,
    capture_production_identity,
)
from scripts.backend_eval.source_binding import HelperExpectation

__all__ = [
    "ARTIFACT_ROOT_BASE_PARTS",
    "CACHE_DIR_NAME",
    "CANDIDATE_NAMES",
    "CANONICAL_REQUIREMENTS_BYTES",
    "LOCK_FILE_NAME",
    "LOCK_ROLLBACK_NAME",
    "QUARANTINE_PREFIX",
    "RECEIPT_FILE_NAME",
    "RECEIPT_ROLLBACK_NAME",
    "REQUIREMENTS_IN_NAME",
    "RESOLUTION_LOCK_NAME",
    "TRANSACTION_MARKER_NAME",
    "CandidateLockError",
    "CandidateLockRequest",
    "CommandResult",
    "CommandRunner",
    "ProductionIdentityChanged",
    "ProductionIdentityError",
    "compile_candidate_lock",
    "subprocess_runner",
]

# Canonical sorted candidate order; the models require sorted, unique names.
CANDIDATE_NAMES = ("pyrefly", "ty")
CANONICAL_REQUIREMENTS_BYTES = b"".join(f"{name}\n".encode() for name in CANDIDATE_NAMES)
ARTIFACT_ROOT_BASE_PARTS = (".admission-artifacts", "backend-eval")
REQUIREMENTS_IN_NAME = "candidate-requirements.in"
LOCK_FILE_NAME = "candidate-requirements.lock"
RECEIPT_FILE_NAME = "candidate-lock-receipt.json"
LOCK_ROLLBACK_NAME = f"{LOCK_FILE_NAME}.rollback"
RECEIPT_ROLLBACK_NAME = f"{RECEIPT_FILE_NAME}.rollback"
TRANSACTION_MARKER_NAME = ".candidate-lock-transaction.json"
QUARANTINE_PREFIX = ".quarantine-"
CACHE_DIR_NAME = "uv-cache"
RESOLUTION_LOCK_NAME = ".candidate-lock.lock"
# Every volatile resolver-owned directory lives below the one rebuildable cache tree.
_CACHE_SUBDIRECTORY_NAMES = ("home", "tmp", "uv", "xdg-cache", "xdg-config")

# (canonical name, durable rollback name, marker key) for every transactional artifact.
_TRANSACTION_ENTRIES = (
    (LOCK_FILE_NAME, LOCK_ROLLBACK_NAME, "lock"),
    (RECEIPT_FILE_NAME, RECEIPT_ROLLBACK_NAME, "receipt"),
)

_EXCLUDE_NEWER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_REQUIREMENT_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)$")
# Final PEP 440 releases only: no pre-release, developmental, or local segment.
_FINAL_VERSION_RE = re.compile(r"^(?:\d+!)?\d+(?:\.\d+)*(?:\.post\d+)?$")
_HASH_RE = re.compile(r"^--hash=sha256:([0-9a-f]{64})$")
_NAME_SEPARATOR_RE = re.compile(r"[-_.]+")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
_NOT_A_REGULAR_FILE = "must be a regular file, not a symlink or special file"
_NOT_A_DIRECTORY = "must be an evaluation-owned directory, not a symlink or special file"


class CandidateLockError(RuntimeError):
    """Raised when the candidate resolution cannot be frozen reproducibly."""


@dataclass(frozen=True, slots=True)
class CandidateLockRequest:
    repo_root: Path
    artifact_root: Path
    uv: Path
    python: Path
    exclude_newer: str

    def __post_init__(self) -> None:
        _validate_directory(self.repo_root, "CandidateLockRequest.repo_root")
        _validate_artifact_root(self.artifact_root, self.repo_root)
        _validate_executable(self.uv, "CandidateLockRequest.uv")
        _validate_executable(self.python, "CandidateLockRequest.python")
        if _EXCLUDE_NEWER_RE.fullmatch(self.exclude_newer) is None:
            raise ValueError(
                "CandidateLockRequest.exclude_newer must be a UTC timestamp such as 2026-08-11T00:00:00Z"
            )


def compile_candidate_lock(
    request: CandidateLockRequest,
    *,
    expectation: HelperExpectation,
    runner: CommandRunner = subprocess_runner,
    recompile: bool = False,
    deadline: Deadline | None = None,
) -> CandidateLock:
    """Return the frozen candidate lock, resolving at most once per artifact root.

    Any transaction left behind by an interrupted call is recovered first.  An existing
    lock is then accepted without a second resolution when recompilation is not requested
    and its canonical receipt matches this request exactly.  A requested recompilation
    must reproduce the frozen bytes exactly and must leave production identity unchanged;
    every failure -- including production drift and an interrupted caller -- either restores
    the prior freeze or leaves no reusable freeze at all.
    """

    before = capture_production_identity(request.repo_root, expectation=expectation, deadline=deadline)
    try:
        with (
            _artifact_directory(request) as dir_fd,
            _resolution_lock(dir_fd, request.artifact_root, deadline),
        ):
            # Under the exclusive lock, any marker still present belongs to a caller that
            # has already exited: a live concurrent transaction can never be rolled back.
            _recover_interrupted_transaction(dir_fd)
            command = _compile_command(request)
            frozen_lock = _read_artifact(dir_fd, LOCK_FILE_NAME)
            frozen_receipt = _read_artifact(dir_fd, RECEIPT_FILE_NAME)
            if frozen_lock is not None and not recompile:
                lock = _accept_frozen_lock(request, command, dir_fd, frozen_lock, frozen_receipt)
                _assert_production_identity_unchanged(
                    before, request.repo_root, expectation, cause=None, deadline=deadline
                )
                return lock
            return _resolve_candidate_lock(
                request, runner, command, dir_fd, before, frozen_lock, frozen_receipt, expectation, deadline
            )
    except BaseException as exc:
        # Drift raised inside the transaction is already the authoritative error.
        if not isinstance(exc, ProductionIdentityError):
            _assert_production_identity_unchanged(
                before, request.repo_root, expectation, cause=exc, deadline=deadline
            )
        raise


def _assert_production_identity_unchanged(
    before: ProductionIdentity,
    repo_root: Path,
    expectation: HelperExpectation,
    *,
    cause: BaseException | None,
    deadline: Deadline | None,
) -> None:
    """Re-check production identity; drift outranks and chains the failure that caused it."""

    try:
        assert_production_identity_unchanged(
            before, capture_production_identity(repo_root, expectation=expectation, deadline=deadline)
        )
    except ProductionIdentityError as identity_error:
        if cause is None:
            raise
        raise identity_error from cause


def _resolve_candidate_lock(
    request: CandidateLockRequest,
    runner: CommandRunner,
    command: Sequence[str],
    dir_fd: int,
    before: ProductionIdentity,
    frozen_lock: bytes | None,
    frozen_receipt: bytes | None,
    expectation: HelperExpectation,
    deadline: Deadline | None = None,
) -> CandidateLock:
    """Resolve once inside a durable transaction that publishes only a validated freeze."""

    marker = {"lock": frozen_lock is not None, "receipt": frozen_receipt is not None}
    _begin_transaction(dir_fd, marker)
    try:
        _write_artifact(dir_fd, REQUIREMENTS_IN_NAME, CANONICAL_REQUIREMENTS_BYTES)
        _ensure_cache_directory(dir_fd)
        result = _run(runner, command, request.artifact_root, deadline)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise CandidateLockError(f"candidate resolution failed ({result.returncode}): {detail}")
        # The canonical output was moved aside before the runner, so anything present now was
        # produced by this resolution.  It is opened exactly once: the bytes that are parsed and
        # the inode that is fsynced are guaranteed to be the same file.
        with _open_regular_artifact(dir_fd, LOCK_FILE_NAME) as lock_fd:
            if lock_fd is None:
                raise CandidateLockError(
                    f"candidate resolution exited 0 but did not write {request.artifact_root / LOCK_FILE_NAME}"
                )
            lock_bytes = _read_descriptor(lock_fd, LOCK_FILE_NAME)
            if frozen_lock is not None and lock_bytes != frozen_lock:
                raise CandidateLockError(
                    "candidate resolution changed after the freeze recorded in "
                    f"{request.artifact_root / LOCK_FILE_NAME}"
                )
            lock = _build_lock(request, lock_bytes)
            # Production identity is validated inside the transaction: the receipt is published
            # only for a resolution that provably left production untouched.
            _assert_production_identity_unchanged(
                before, request.repo_root, expectation, cause=None, deadline=deadline
            )
            # The lock contents must be durable before anything published depends on them.
            _fsync_file(lock_fd, LOCK_FILE_NAME)
        _write_artifact(dir_fd, RECEIPT_FILE_NAME, _receipt_bytes(command, CANONICAL_REQUIREMENTS_BYTES, lock))
    except BaseException as exc:
        _rollback_or_report(dir_fd, marker, cause=exc)
        raise
    _commit_transaction(dir_fd)
    return lock


def _rollback_or_report(dir_fd: int, marker: Mapping[str, bool], *, cause: BaseException) -> None:
    try:
        _rollback_transaction(dir_fd, marker)
    except CandidateLockError as rollback_error:
        raise CandidateLockError(
            f"candidate resolution failed and its frozen artifacts could not be restored: {rollback_error}"
        ) from cause


# --- durable artifact transaction ---------------------------------------------


def _begin_transaction(dir_fd: int, marker: Mapping[str, bool]) -> None:
    """Record the prior state, then move the existing artifacts to durable rollback entries."""

    _write_artifact(dir_fd, TRANSACTION_MARKER_NAME, canonical_json(dict(marker)))
    _fsync_directory(dir_fd)
    for canonical, rollback, key in _TRANSACTION_ENTRIES:
        if marker[key]:
            _purge_node(dir_fd, rollback)
            _rename_artifact(dir_fd, canonical, rollback)
            # The backup contents -- not just the rename -- must be durable before the runner
            # can touch the canonical names.
            _fsync_regular_artifact(dir_fd, rollback)
    _fsync_directory(dir_fd)


def _commit_transaction(dir_fd: int) -> None:
    """Clear the marker and drop the backups once the new canonical state is durable.

    The caller has already fsynced the lock file, written and fsynced the receipt file, and
    validated production identity; only the directory entries still need to be made durable.
    """

    _fsync_directory(dir_fd)
    _remove_artifact(dir_fd, TRANSACTION_MARKER_NAME)
    _fsync_directory(dir_fd)
    for _canonical, rollback, _key in _TRANSACTION_ENTRIES:
        _purge_node(dir_fd, rollback)


def _rollback_transaction(dir_fd: int, marker: Mapping[str, bool]) -> None:
    """Restore the recorded prior state without ever deleting the only durable copy."""

    quarantined: list[str] = []
    for canonical, rollback, key in _TRANSACTION_ENTRIES:
        if not marker[key]:
            # The artifact did not exist before this transaction; nothing may survive it.
            quarantined.extend(_quarantine_nodes(dir_fd, canonical, rollback))
        elif _node_exists(dir_fd, rollback):
            quarantined.extend(_quarantine_nodes(dir_fd, canonical))
            _rename_artifact(dir_fd, rollback, canonical)
        # A missing rollback entry with marker[key] set means the move has not happened yet
        # or has already been undone: the canonical artifact is the original one.
    _fsync_directory(dir_fd)
    _remove_artifact(dir_fd, TRANSACTION_MARKER_NAME)
    _fsync_directory(dir_fd)
    for name in quarantined:
        _purge_node(dir_fd, name)


def _recover_interrupted_transaction(dir_fd: int) -> None:
    """Recover a transaction left behind by an interrupted prior call."""

    _purge_quarantined_nodes(dir_fd)
    marker_bytes = _read_artifact(dir_fd, TRANSACTION_MARKER_NAME)
    if marker_bytes is None:
        # Without a marker the canonical state is authoritative and any rollback entry is
        # a leftover of a completed commit.
        for _canonical, rollback, _key in _TRANSACTION_ENTRIES:
            _purge_node(dir_fd, rollback)
        return
    _rollback_transaction(dir_fd, _decode_marker(marker_bytes))


def _purge_quarantined_nodes(dir_fd: int) -> None:
    """Drop quarantined nodes left behind by a call interrupted mid-rollback."""

    try:
        with os.scandir(dir_fd) as entries:
            stale = [entry.name for entry in entries if entry.name.startswith(QUARANTINE_PREFIX)]
    except OSError as exc:
        raise CandidateLockError(f"cannot scan the artifact directory: {exc}") from exc
    for name in stale:
        _purge_node(dir_fd, name)


def _decode_marker(marker_bytes: bytes) -> dict[str, bool]:
    """Decode the transaction marker, defaulting unknown entries to "existed before".

    The conservative default never deletes a canonical artifact the marker cannot vouch for.
    """

    try:
        decoded = json.loads(marker_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        decoded = {}
    if not isinstance(decoded, Mapping):
        decoded = {}
    return {key: bool(decoded.get(key, True)) for _canonical, _rollback, key in _TRANSACTION_ENTRIES}


def _run(
    runner: CommandRunner, command: Sequence[str], artifact_root: Path, deadline: Deadline | None
) -> CommandResult:
    timeout = None if deadline is None else deadline.remaining()
    try:
        return runner(command, cwd=artifact_root, env=_command_env(artifact_root), timeout=timeout)
    except CommandTimeout as exc:
        raise CandidateLockError(f"the candidate resolution command timed out: {exc}") from exc
    except OSError as exc:
        raise CandidateLockError(f"cannot start the candidate resolution command: {exc}") from exc


@contextmanager
def _resolution_lock(dir_fd: int, artifact_root: Path, deadline: Deadline | None) -> Iterator[None]:
    """Serialize every caller of one artifact identity on an ``O_NOFOLLOW`` lock file.

    Acquisition is bounded by the phase deadline: waiting for another resolver is a step
    like any other and may not push the run past its ceiling.
    """

    try:
        fd = os.open(
            RESOLUTION_LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dir_fd
        )
    except OSError as exc:
        raise CandidateLockError(
            f"cannot open the candidate resolution lock {artifact_root / RESOLUTION_LOCK_NAME}: {exc}"
        ) from exc
    try:
        os.fchmod(fd, 0o600)
        try:
            acquire_exclusive_lock(fd, deadline=deadline, step="candidate_resolution_lock")
        except OSError as exc:
            raise CandidateLockError(f"cannot lock {artifact_root / RESOLUTION_LOCK_NAME}: {exc}") from exc
        yield
    finally:
        os.close(fd)


# --- command and environment --------------------------------------------------


def _compile_command(request: CandidateLockRequest) -> tuple[str, ...]:
    return (
        str(request.uv),
        "pip",
        "compile",
        str(request.artifact_root / REQUIREMENTS_IN_NAME),
        "--output-file",
        str(request.artifact_root / LOCK_FILE_NAME),
        "--generate-hashes",
        "--no-annotate",
        "--no-header",
        "--resolution",
        "highest",
        "--prerelease",
        "disallow",
        "--only-binary",
        ":all:",
        "--python",
        str(request.python),
        "--no-sources",
        "--no-python-downloads",
        "--exclude-newer",
        request.exclude_newer,
    )


def _command_env(artifact_root: Path) -> dict[str, str]:
    """Keep bootstrap's external-network proxy and nothing else ambient."""

    cache = artifact_root / CACHE_DIR_NAME
    return bootstrap_environment(
        bootstrap_service_values(
            home=cache / "home",
            tmp=cache / "tmp",
            cache=cache / "xdg-cache",
            config=cache / "xdg-config",
            uv_cache=cache / "uv",
        )
    )


# --- artifact directory and file handling -------------------------------------


@contextmanager
def _artifact_directory(request: CandidateLockRequest) -> Iterator[int]:
    """Yield a descriptor for the artifact root, refusing any symlinked component.

    The declared production root is opened as given; every component below it is
    created and reopened with ``O_NOFOLLOW`` so no symlink can move the artifact
    root, its parents, or its files outside the evaluation-owned area.
    """

    relative = request.artifact_root.relative_to(request.repo_root)
    try:
        dir_fd = os.open(request.repo_root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise CandidateLockError(f"cannot open the production repository root {request.repo_root}: {exc}") from exc
    try:
        for part in relative.parts:
            child = _open_owned_directory(dir_fd, part)
            os.close(dir_fd)
            dir_fd = child
        yield dir_fd
    finally:
        os.close(dir_fd)


def _open_owned_directory(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise CandidateLockError(f"cannot create artifact path component {name!r}: {exc}") from exc
    try:
        fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise CandidateLockError(f"artifact path component {name!r} {_NOT_A_DIRECTORY}: {exc}") from exc
    # ``mkdir`` is masked by the ambient umask; the service-owned mode is not negotiable.
    try:
        os.fchmod(fd, 0o700)
    except OSError as exc:
        os.close(fd)
        raise CandidateLockError(f"cannot own artifact path component {name!r}: {exc}") from exc
    return fd


def _ensure_cache_directory(dir_fd: int) -> None:
    """Create the one rebuildable resolver store and every volatile directory below it."""

    cache_fd = _open_owned_directory(dir_fd, CACHE_DIR_NAME)
    try:
        for name in _CACHE_SUBDIRECTORY_NAMES:
            os.close(_open_owned_directory(cache_fd, name))
    finally:
        os.close(cache_fd)


@contextmanager
def _open_regular_artifact(dir_fd: int, name: str) -> Iterator[int | None]:
    """Open ``name`` relative to ``dir_fd`` without following links.

    Yields a descriptor for a verified regular file, or ``None`` when the artifact does not
    exist.  Every read and every file fsync goes through this seam, so callers can prove they
    acted on one specific inode.
    """

    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        yield None
        return
    except OSError as exc:
        raise CandidateLockError(f"artifact {name!r} {_NOT_A_REGULAR_FILE}: {exc}") from exc
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            raise CandidateLockError(f"artifact {name!r} must be a regular file, not a {_node_kind(mode)}")
        yield fd
    finally:
        os.close(fd)


def _read_artifact(dir_fd: int, name: str) -> bytes | None:
    """Return the artifact bytes, or ``None`` when it does not exist."""

    with _open_regular_artifact(dir_fd, name) as fd:
        return None if fd is None else _read_descriptor(fd, name)


def _read_descriptor(fd: int, name: str) -> bytes:
    try:
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read()
    except OSError as exc:
        raise CandidateLockError(f"cannot read artifact {name!r}: {exc}") from exc


def _fsync_file(fd: int, name: str) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        raise CandidateLockError(f"cannot fsync artifact {name!r}: {exc}") from exc


def _fsync_regular_artifact(dir_fd: int, name: str) -> None:
    """Make one existing regular artifact's contents durable, refusing any other node type."""

    with _open_regular_artifact(dir_fd, name) as fd:
        if fd is None:
            raise CandidateLockError(f"artifact {name!r} disappeared before it could be made durable")
        _fsync_file(fd, name)


def _node_kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "special file"


def _write_artifact(dir_fd: int, name: str, data: bytes) -> None:
    """Atomically replace ``name`` with ``data`` without ever following a symlink."""

    _reject_non_regular(dir_fd, name)
    temporary = _temporary_name(name)
    _remove_artifact(dir_fd, temporary)
    try:
        fd = os.open(temporary, _CREATE_FLAGS, 0o600, dir_fd=dir_fd)
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            _fsync_file(handle.fileno(), temporary)
        os.replace(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError as exc:
        raise CandidateLockError(f"cannot write artifact {name!r}: {exc}") from exc


def _temporary_name(name: str) -> str:
    return f".{name}.tmp"


def _reject_non_regular(dir_fd: int, name: str) -> None:
    try:
        mode = os.lstat(name, dir_fd=dir_fd).st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CandidateLockError(f"cannot inspect artifact {name!r}: {exc}") from exc
    if not stat.S_ISREG(mode):
        raise CandidateLockError(f"artifact {name!r} must be a regular file, not a {_node_kind(mode)}")


def _remove_artifact(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CandidateLockError(f"cannot remove artifact {name!r}: {exc}") from exc


def _rename_artifact(dir_fd: int, source: str, target: str) -> None:
    """Atomically move ``source`` onto ``target`` inside the artifact directory."""

    try:
        os.rename(source, target, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except OSError as exc:
        raise CandidateLockError(f"cannot move artifact {source!r} to {target!r}: {exc}") from exc


def _node_exists(dir_fd: int, name: str) -> bool:
    try:
        os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CandidateLockError(f"cannot inspect artifact {name!r}: {exc}") from exc
    return True


def _quarantine_nodes(dir_fd: int, *names: str) -> list[str]:
    quarantined: list[str] = []
    for name in names:
        moved = _quarantine_node(dir_fd, name)
        if moved is not None:
            quarantined.append(moved)
    return quarantined


def _quarantine_node(dir_fd: int, name: str) -> str | None:
    """Rename any node -- file, symlink, special file, or directory -- out of the way.

    Renaming rather than deleting keeps the durable copy intact until the restored
    canonical state has been fsynced, and never follows the node it moves.
    """

    if not _node_exists(dir_fd, name):
        return None
    quarantine = f"{QUARANTINE_PREFIX}{name}-{uuid.uuid4().hex}"
    _rename_artifact(dir_fd, name, quarantine)
    return quarantine


def _purge_node(dir_fd: int, name: str) -> None:
    """Remove a node of any type relative to ``dir_fd`` without following links."""

    try:
        mode = os.lstat(name, dir_fd=dir_fd).st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CandidateLockError(f"cannot inspect artifact {name!r}: {exc}") from exc
    if stat.S_ISDIR(mode):
        _purge_directory(dir_fd, name)
        return
    _remove_artifact(dir_fd, name)


def _purge_directory(parent_fd: int, name: str) -> None:
    try:
        fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise CandidateLockError(f"cannot open artifact directory {name!r}: {exc}") from exc
    try:
        with os.scandir(fd) as entries:
            children = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in entries]
        for child, is_directory in children:
            if is_directory:
                _purge_directory(fd, child)
            else:
                _remove_artifact(fd, child)
    except OSError as exc:
        raise CandidateLockError(f"cannot clear artifact directory {name!r}: {exc}") from exc
    finally:
        os.close(fd)
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except OSError as exc:
        raise CandidateLockError(f"cannot remove artifact directory {name!r}: {exc}") from exc


def _fsync_directory(dir_fd: int) -> None:
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        raise CandidateLockError(f"cannot fsync the artifact directory: {exc}") from exc


# --- artifact-root inputs and receipts ----------------------------------------


def _require_canonical_requirements_input(dir_fd: int) -> bytes:
    data = _read_artifact(dir_fd, REQUIREMENTS_IN_NAME)
    if data != CANONICAL_REQUIREMENTS_BYTES:
        raise CandidateLockError(
            f"candidate requirements input must be exactly {CANONICAL_REQUIREMENTS_BYTES!r}, got {data!r}"
        )
    return CANONICAL_REQUIREMENTS_BYTES


def _receipt_bytes(command: Sequence[str], requirements_bytes: bytes, lock: CandidateLock) -> bytes:
    return canonical_json(
        {
            "candidate_lock_digest": lock.digest,
            "candidate_names": list(CANDIDATE_NAMES),
            "command": list(command),
            "exclude_newer": lock.exclude_newer,
            "requirements_in_sha256": sha256_bytes(requirements_bytes),
        }
    )


def _accept_frozen_lock(
    request: CandidateLockRequest,
    command: Sequence[str],
    dir_fd: int,
    frozen_lock: bytes,
    frozen_receipt: bytes | None,
) -> CandidateLock:
    requirements_bytes = _require_canonical_requirements_input(dir_fd)
    if frozen_receipt is None:
        raise CandidateLockError(
            f"frozen candidate lock is missing its canonical receipt: {request.artifact_root / RECEIPT_FILE_NAME}"
        )
    lock = _build_lock(request, frozen_lock)
    if frozen_receipt != _receipt_bytes(command, requirements_bytes, lock):
        raise CandidateLockError(
            f"frozen candidate lock does not match its canonical receipt: "
            f"{request.artifact_root / RECEIPT_FILE_NAME}"
        )
    return lock


# --- resolution parsing -------------------------------------------------------


def _build_lock(request: CandidateLockRequest, lock_bytes: bytes) -> CandidateLock:
    resolved_packages = _parse_resolution(lock_bytes)
    resolved_by_name = {package.name: package for package in resolved_packages}
    missing = [name for name in CANDIDATE_NAMES if name not in resolved_by_name]
    if missing:
        raise CandidateLockError(f"candidate resolution is missing required candidates: {', '.join(missing)}")
    candidates = tuple(
        CandidatePackage(
            name=resolved_by_name[name].name,
            version=resolved_by_name[name].version,
            requirement=resolved_by_name[name].requirement,
            artifact_hashes=resolved_by_name[name].artifact_hashes,
            executable_relpath=f"bin/{name}",
        )
        for name in CANDIDATE_NAMES
    )
    digest = sha256_bytes(lock_bytes)
    return CandidateLock(
        digest=digest,
        exclude_newer=request.exclude_newer,
        resolved_packages=resolved_packages,
        candidates=candidates,
        # The structured fields cannot reproduce a raw file's bytes; the witness says so.
        lock_evidence=LockEvidence.build(
            raw_sha256=digest, raw_size=len(lock_bytes), resolved_packages=resolved_packages
        ),
    )


def _parse_resolution(lock_bytes: bytes) -> tuple[ResolvedPackage, ...]:
    try:
        text = lock_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CandidateLockError(f"candidate resolution is not valid UTF-8: {exc}") from exc
    packages: list[ResolvedPackage] = []
    seen: set[str] = set()
    for line in _logical_lines(text):
        if line.startswith("#"):
            continue
        package = _parse_requirement_line(line)
        if package.name in seen:
            raise CandidateLockError(f"candidate resolution contains a duplicate package: {package.name}")
        seen.add(package.name)
        packages.append(package)
    if not packages:
        raise CandidateLockError("candidate resolution is empty")
    return tuple(sorted(packages, key=lambda package: package.name))


def _parse_requirement_line(line: str) -> ResolvedPackage:
    if ";" in line:
        raise CandidateLockError(f"candidate resolution rejects an environment marker: {line}")
    if line.startswith(("-e ", "--editable")):
        raise CandidateLockError(f"candidate resolution rejects an editable requirement: {line}")
    tokens = line.split()
    requirement = tokens[0]
    if requirement.startswith("-"):
        raise CandidateLockError(f"candidate resolution rejects an unexpected requirement option: {line}")
    if "@" in requirement or "://" in line or requirement.startswith(("http", "file:")):
        raise CandidateLockError(f"candidate resolution rejects a direct URL requirement: {line}")
    match = _REQUIREMENT_RE.fullmatch(requirement)
    if match is None:
        raise CandidateLockError(f"candidate resolution requires an exact == pin: {line}")
    name = _NAME_SEPARATOR_RE.sub("-", match.group(1)).lower()
    version = match.group(2)
    if _FINAL_VERSION_RE.fullmatch(version) is None:
        raise CandidateLockError(
            f"candidate resolution rejects a pre-release, developmental, or local version: {requirement}"
        )
    return ResolvedPackage(
        name=name,
        version=version,
        requirement=f"{name}=={version}",
        artifact_hashes=_parse_hashes(name, tokens[1:]),
    )


def _parse_hashes(name: str, tokens: Sequence[str]) -> tuple[str, ...]:
    digests: list[str] = []
    for token in tokens:
        match = _HASH_RE.fullmatch(token)
        if match is None:
            raise CandidateLockError(
                f"candidate resolution requires --hash=sha256:<digest> entries for {name}: {token}"
            )
        digest = match.group(1)
        if digest in digests:
            raise CandidateLockError(f"candidate resolution contains a duplicate hash for {name}: {digest}")
        digests.append(digest)
    if not digests:
        raise CandidateLockError(f"candidate resolution is missing artifact hashes for {name}")
    return tuple(sorted(digests))


def _logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.endswith("\\"):
            buffer += f"{stripped[:-1].strip()} "
            continue
        buffer += stripped
        if buffer:
            lines.append(buffer)
        buffer = ""
    if buffer.strip():
        lines.append(buffer.strip())
    return lines


# --- request validation -------------------------------------------------------


def _validate_directory(path: Path, label: str) -> None:
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    if not path.is_dir():
        raise ValueError(f"{label} must be an existing directory: {path}")


def _validate_executable(path: Path, label: str) -> None:
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path, not an ambient executable name")
    if not path.is_file():
        raise ValueError(f"{label} must be an existing file: {path}")
    if not os.access(path, os.X_OK):
        raise ValueError(f"{label} must be an executable file: {path}")


def _validate_artifact_root(artifact_root: Path, repo_root: Path) -> None:
    base = repo_root.joinpath(*ARTIFACT_ROOT_BASE_PARTS)
    if not artifact_root.is_absolute():
        raise ValueError("CandidateLockRequest.artifact_root must be an absolute path")
    if ".." in artifact_root.parts:
        raise ValueError("CandidateLockRequest.artifact_root must not contain parent references")
    if artifact_root == base or not artifact_root.is_relative_to(base):
        raise ValueError(f"CandidateLockRequest.artifact_root must be an evaluation-owned directory below {base}")
