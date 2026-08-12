"""Publish one immutable record atomically, durably, and inside a phase ceiling.

This is the low-level primitive Phase 1 admission publishes its receipt through, extracted
so a later phase can publish its own record on exactly the same guarantees rather than
reimplementing them.  It owns no evaluation semantics: it is given an already-serialized
payload, the names to publish it under, and the phase's monotonic deadline.

**The final name is never overwritten.**  The payload is written to a per-identity temporary
created ``O_EXCL`` and then *linked* -- not renamed -- onto the canonical name, so an
existing record survives even a repeated identity.  Publication is serialized on a
per-target ``O_NOFOLLOW`` lock acquired non-blockingly against the same deadline.

**The whole publication is inside the ceiling, not after it.**  A record can be tens of
megabytes: writing, ``fsync``-ing, and linking it takes real time, and the lock can be held
by another process.  Every one of those steps is therefore checked against the same absolute
deadline -- the lock poll, each write chunk, and a publication reserve that must still remain
in front of the atomic ``link``.

**After the link, every step is followed by a checkpoint.**  Each remaining namespace
mutation and each durability barrier is followed by a :class:`_Publication` observation of
the ceiling, including one immediately before the primitive returns, while withdrawal is
still possible; only descriptor closes follow it, and they touch neither the namespace nor
storage.  Any checkpoint that observes expiry withdraws this run's own link -- and only its
own, which ``O_EXCL`` and the failing ``link`` prove no other run published -- and fails.

**What that does and does not promise.**  The deadline is enforced *cooperatively*, at the
boundaries between syscalls; a filesystem call already in flight is not preemptible.  Between
``link`` returning and the next checkpoint -- for as long as one in-flight ``fsync`` takes --
the final name exists in the directory even if the ceiling has already passed.  That entry is
withdrawn as soon as expiry is observed, and it is not admitted evidence: a consumer requires
the command to have completed successfully and the record to verify canonically against its
own digest, and an overrun supplies neither.  This is a kernel boundary, not a guarantee of
zero transient visibility.

Failures are typed rather than phrased in any one phase's vocabulary: every one of them is a
:class:`PublicationError` carrying :data:`PUBLICATION_FAILED` or
:data:`PUBLICATION_DEADLINE_EXCEEDED`.  A phase adapter maps those two codes onto its own
disposition and passes the detail through unchanged; the detail's nouns come from the
request, so an adapter that keeps the defaults publishes exactly the text Phase 1 always did.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from scripts.backend_eval.process import Deadline, DeadlineExceeded, acquire_exclusive_lock

__all__ = [
    "PUBLICATION_DEADLINE_EXCEEDED",
    "PUBLICATION_FAILED",
    "PUBLICATION_RESERVE_SECONDS",
    "PublicationError",
    "PublicationFailure",
    "PublicationRequest",
    "publish_immutable_record",
]

# The window the atomic link and its directory ``fsync`` must still have in front of them.
PUBLICATION_RESERVE_SECONDS = 5.0

PUBLICATION_FAILED = "publication_failed"
"""The filesystem refused a publication step, or the final name already exists."""

PUBLICATION_DEADLINE_EXCEEDED = "publication_deadline_exceeded"
"""The phase ceiling arrived; nothing was published, and anything linked was withdrawn."""

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY
_NOFOLLOW_DIRECTORY_FLAGS = _DIRECTORY_FLAGS | os.O_NOFOLLOW
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
# One record is tens of megabytes; writing it in chunks keeps the ceiling observable.
_WRITE_CHUNK_BYTES = 4 * 1024 * 1024
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600


# --- typed failures ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PublicationFailure:
    """The typed disposition of one publication that did not complete cleanly."""

    code: str
    detail: str

    def __post_init__(self) -> None:
        if self.code not in {PUBLICATION_FAILED, PUBLICATION_DEADLINE_EXCEEDED}:
            raise ValueError(
                "PublicationFailure.code must be "
                f"{PUBLICATION_FAILED!r} or {PUBLICATION_DEADLINE_EXCEEDED!r}"
            )
        if not self.detail:
            raise ValueError("PublicationFailure.detail must be a non-empty string")


class PublicationError(RuntimeError):
    """Raised when a record could not be published; carries the typed disposition."""

    def __init__(self, failure: PublicationFailure) -> None:
        super().__init__(f"{failure.code}: {failure.detail}")
        self.failure = failure


def _fail(code: str, detail: str) -> PublicationError:
    return PublicationError(PublicationFailure(code=code, detail=detail))


@contextmanager
def _translated_deadline() -> Iterator[None]:
    """One cooperative deadline observation, reported in this primitive's vocabulary."""

    try:
        yield
    except DeadlineExceeded as exc:
        raise _fail(PUBLICATION_DEADLINE_EXCEEDED, str(exc)) from exc


# --- the request ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PublicationRequest:
    """Everything one immutable publication needs, and nothing about what it means.

    ``owner_root`` is the already-declared root the descriptor walk starts from, and
    ``target_root`` is the directory below it that owns ``directory_name`` and the lock.
    Every name is a single path component resolved relative to a descriptor, never a
    pathname, so no ancestor substitution can move the target after it was proven.
    """

    owner_root: Path
    target_root: Path
    directory_name: str
    lock_name: str
    identity: str
    entry_name: str
    temporary_name: str
    payload: bytes
    noun: str = "receipt"
    step_prefix: str = "publish_receipt"
    reserve_seconds: float = PUBLICATION_RESERVE_SECONDS

    def __post_init__(self) -> None:
        _require_absolute(self.owner_root, "PublicationRequest.owner_root")
        _require_absolute(self.target_root, "PublicationRequest.target_root")
        _require_component(self.directory_name, "PublicationRequest.directory_name")
        _require_component(self.lock_name, "PublicationRequest.lock_name")
        _require_component(self.entry_name, "PublicationRequest.entry_name")
        _require_component(self.temporary_name, "PublicationRequest.temporary_name")
        if self.entry_name == self.temporary_name:
            raise ValueError("PublicationRequest.temporary_name must differ from entry_name")
        for label, value in (("identity", self.identity), ("noun", self.noun), ("step_prefix", self.step_prefix)):
            if not value:
                raise ValueError(f"PublicationRequest.{label} must be a non-empty string")


def _require_absolute(path: Path, label: str) -> None:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    if ".." in path.parts:
        raise ValueError(f"{label} must not contain parent references")


def _require_component(name: str, label: str) -> None:
    """A name handed to a ``dir_fd`` call must be one component, or it is a pathname."""

    if not name or "/" in name or name in {".", ".."}:
        raise ValueError(f"{label} must be a single path component")


# --- the primitive ------------------------------------------------------------------


def publish_immutable_record(request: PublicationRequest, deadline: Deadline) -> Path:
    """Write ``request.payload`` exclusively and durably, never replacing another record.

    Returns the published path.  Raises :class:`PublicationError` for every failure,
    including an expired ceiling, after withdrawing anything this call itself linked.
    """

    temporary = request.temporary_name
    final = request.entry_name
    owner_fd = _open_target_directory(request)
    try:
        with _publication_lock(owner_fd, request, deadline):
            directory_fd = _open_owned_child(owner_fd, request.directory_name, request)
            try:
                _stage_payload(directory_fd, request, deadline)
                _require_publication_window(directory_fd, request, deadline)
                try:
                    os.link(temporary, final, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
                except FileExistsError as exc:
                    # The link is refused, so this call published nothing -- and it must
                    # leave nothing either.  The owning phase's cleanup runs *before*
                    # publication, so a temporary left here would never be collected.
                    _discard_temporary(directory_fd, request)
                    raise _fail(
                        PUBLICATION_FAILED,
                        f"a {request.noun} for run {request.identity} already exists and is immutable",
                    ) from exc
                except OSError as exc:
                    _discard_temporary(directory_fd, request)
                    raise _fail(
                        PUBLICATION_FAILED,
                        f"cannot publish the {request.noun} below {request.target_root}: {exc}",
                    ) from exc
                # From here the final name exists.  Every remaining namespace mutation and
                # every durability barrier is followed by an expiry observation, and each
                # observation can still withdraw this call's own link, so no step after the
                # link can carry a published record past the ceiling.
                published = _Publication(directory_fd, request, deadline)
                try:
                    _sync_directory(directory_fd, request)
                    published.checkpoint("link_synced")
                    _replace_temporary(directory_fd, request)
                    published.checkpoint("temporary_unlinked")
                    _sync_directory(directory_fd, request)
                    published.checkpoint("temporary_unlink_synced")
                    # Immediately before returning, while withdrawal is still possible: only
                    # descriptor closes remain, and they touch neither the namespace nor
                    # storage.
                    published.checkpoint("return")
                except PublicationError as exc:
                    published.recover(exc)
            finally:
                # Post-durability.  The link is made, both barriers have returned, and this
                # descriptor carries no payload of its own: releasing it cannot un-publish
                # what the barriers already proved.
                _release_descriptor(directory_fd)
    finally:
        _release_descriptor(owner_fd)
    return request.target_root / request.directory_name / final


def _require_publication_window(directory_fd: int, request: PublicationRequest, deadline: Deadline) -> None:
    """Refuse to link a record that cannot be completed inside the ceiling."""

    if deadline.remaining() > request.reserve_seconds:
        return
    _discard_temporary(directory_fd, request)
    raise _fail(
        PUBLICATION_DEADLINE_EXCEEDED,
        f"step={request.step_prefix}:link elapsed={deadline.elapsed():.3f}s budget={deadline.seconds:g}s "
        f"reserve={request.reserve_seconds:g}s; the {request.noun} was not published",
    )


# The clause every withdrawal detail opens with, naming why the record is being removed.
_CEILING_REASON = "the ceiling was reached during publication"


@dataclass(slots=True)
class _Publication:
    """One linked record this call owns, and the two ways it can still be taken back.

    A checkpoint is what happens between two post-link operations: it asks the same monotonic
    deadline whether the ceiling has arrived and, if it has, withdraws the very link this call
    created before failing, so a call that overran leaves no record and returns none.

    A post-link *failure* is the other way.  A durability barrier or the temporary unlink can
    fail outright, and a typed failure that propagated while the canonical name stayed on disk
    would be a publication that reported failure and left readable evidence behind.
    :meth:`recover` closes that: the same owned-name withdrawal runs, and the original typed
    failure is re-raised only once the removal is proven.

    Both paths remove exactly the two names this call owns, which ``O_EXCL`` and the failing
    ``link`` prove no other call published.  The withdrawal is attempted at most once, so a
    checkpoint that already withdrew is not re-run by the recovery and its ceiling disposition
    is never re-worded as a failure.
    """

    directory_fd: int
    request: PublicationRequest
    deadline: Deadline
    withdrawal_attempted: bool = False

    def checkpoint(self, step: str) -> None:
        if not self.deadline.expired():
            return
        self.withdraw(step)

    def withdraw(self, step: str) -> NoReturn:
        """The ceiling arrived: remove this call's own names, then fail closed."""

        self._remove_owned_names(_CEILING_REASON)
        raise _fail(
            PUBLICATION_DEADLINE_EXCEEDED,
            f"step={self.request.step_prefix}:{step} elapsed={self.deadline.elapsed():.3f}s "
            f"budget={self.deadline.seconds:g}s; the {self.request.noun} was withdrawn and none was published",
        )

    def recover(self, failure: PublicationError) -> NoReturn:
        """A post-link step failed: leave no canonical name, then re-raise the original.

        The original disposition is what the caller gets back whenever the withdrawal is
        proven -- the failure that stopped the publication, not the cleanup that followed it.
        When the withdrawal itself cannot be completed or proven durable, that is reported
        instead, because the state it leaves behind is the one the caller has to know about.
        """

        if not self.withdrawal_attempted:
            self._remove_owned_names(f"the {self.request.noun} publication failed")
        raise failure

    def _remove_owned_names(self, reason: str) -> None:
        """Remove this call's own names -- and only its own -- and prove the removal durable.

        Both unlinks are attempted before the barrier, so a name that *can* be removed is
        still removed and still forced to storage when the other one cannot be; the first
        failure is reported afterwards.  An unproven withdrawal outranks it: if the barrier
        fails, nothing about the directory's contents can be claimed, and saying so is the
        only honest answer.
        """

        self.withdrawal_attempted = True
        refused: PublicationError | None = None
        for name in (self.request.entry_name, self.request.temporary_name):
            try:
                os.unlink(name, dir_fd=self.directory_fd)
            except FileNotFoundError:
                continue
            except OSError as exc:
                if refused is None:
                    refused = _fail(
                        PUBLICATION_FAILED,
                        f"{reason} and {name} below {self.request.target_root} "
                        f"could not be withdrawn: {exc}",
                    )
        try:
            _sync_directory(self.directory_fd, self.request)
        except PublicationError as exc:
            raise _fail(
                PUBLICATION_FAILED,
                f"{reason} and the withdrawal of the {self.request.noun} below "
                f"{self.request.target_root} could not be proven durable: {exc.failure.detail}",
            ) from exc
        if refused is not None:
            raise refused


def _stage_payload(directory_fd: int, request: PublicationRequest, deadline: Deadline) -> None:
    """Create, own, write, synchronize, and close this call's own temporary, before any link.

    Everything here is *pre-link*: nothing is published yet, so every refusal costs only this
    call's own temporary, which is discarded and its removal proven durable before the failure
    is reported.  Each step is named in the payload's own words rather than borrowed from a
    neighbouring one -- the close in particular, which is the last thing that happens before
    the link and used to escape this handling entirely.
    """

    _replace_temporary(directory_fd, request)
    try:
        file_fd = os.open(request.temporary_name, _CREATE_FLAGS, _FILE_MODE, dir_fd=directory_fd)
    except OSError as exc:
        _discard_temporary(directory_fd, request)
        raise _fail(
            PUBLICATION_FAILED,
            f"cannot create the {request.noun} temporary below {request.target_root}: {exc}",
        ) from exc
    try:
        os.fchmod(file_fd, _FILE_MODE)
        _write_all(file_fd, request, deadline)
        os.fsync(file_fd)
    except OSError as exc:
        _release_descriptor(file_fd)
        _discard_temporary(directory_fd, request)
        raise _fail(
            PUBLICATION_FAILED, f"cannot write the {request.noun} below {request.target_root}: {exc}"
        ) from exc
    except BaseException:
        # A half-written record is never left behind, not even under a dot name.  The typed
        # failure that got here already says what went wrong, so it is re-raised unchanged.
        _release_descriptor(file_fd)
        _discard_temporary(directory_fd, request)
        raise
    _close_payload(file_fd, directory_fd, request)


def _close_payload(file_fd: int, directory_fd: int, request: PublicationRequest) -> None:
    """Close the payload descriptor as a pre-link step whose refusal is still actionable.

    This is the one close in this module the caller can do something about: the link has not
    happened, so a refusal means nothing was published and the temporary must go.  The
    descriptor itself is gone either way -- Linux releases it whether or not ``close`` reports
    a deferred error -- so there is nothing to retry, only the abandoned temporary to remove.
    """

    try:
        os.close(file_fd)
    except OSError as exc:
        _discard_temporary(directory_fd, request)
        raise _fail(
            PUBLICATION_FAILED, f"cannot close the {request.noun} below {request.target_root}: {exc}"
        ) from exc


def _release_descriptor(fd: int) -> None:
    """Release a descriptor whose close can no longer change what has already been decided.

    Linux frees the descriptor unconditionally.  A ``close`` that returns an error is
    reporting a *deferred* writeback failure the kernel already recorded; the descriptor is
    gone regardless, so retrying is unsafe -- the number may already have been reused -- and
    there is no state left to repair.

    Every call site is one where the outcome is already settled.  The publication directory
    and the owner root are read-only descriptors whose one durability barrier was an explicit
    ``fsync`` that already returned; the publication lock carries no payload of its own; the
    walk releases a parent only once its child is open; and the payload descriptor arrives
    here only on a path that is already failing and already discarding the temporary.  Letting
    a close error escape any of them would hand the caller a failure while the durable record
    it denies is on disk and readable -- the exact defect this module exists to prevent -- or
    would mask the failure that was already being reported.

    This is therefore the only place in this module where a filesystem error is deliberately
    not propagated, and it is stated as a residual boundary in the ownership document rather
    than left implicit.
    """

    with suppress(OSError):
        os.close(fd)


def _discard_temporary(dir_fd: int, request: PublicationRequest) -> None:
    """Remove this call's own temporary and prove the removal durable.

    Every path that abandons a publication before the link shares this, so no abandoned
    attempt can leave a dot-name behind for a cleanup that has already run to collect.
    """

    _replace_temporary(dir_fd, request)
    _sync_directory(dir_fd, request)


def _sync_directory(dir_fd: int, request: PublicationRequest) -> None:
    """One durability barrier for a publication-directory mutation.

    Named rather than inlined so that a test can make exactly this barrier slow and prove
    the checkpoint after it still refuses -- and withdraws -- a record that crossed the
    ceiling while the barrier was running.
    """

    try:
        os.fsync(dir_fd)
    except OSError as exc:
        raise _fail(
            PUBLICATION_FAILED,
            f"cannot synchronize the {request.directory_name} directory below {request.target_root}: {exc}",
        ) from exc


@contextmanager
def _publication_lock(owner_fd: int, request: PublicationRequest, deadline: Deadline) -> Iterator[None]:
    try:
        fd = os.open(
            request.lock_name,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            _FILE_MODE,
            dir_fd=owner_fd,
        )
    except OSError as exc:
        raise _fail(
            PUBLICATION_FAILED,
            f"cannot open the publication lock below {request.target_root}: {exc}",
        ) from exc
    try:
        # Only this lock's own acquisition is named ``cannot lock``.  The yielded body is
        # deliberately outside this handler: every failure the publication itself raises is
        # already typed in its own words, and relabelling one of them as a locking failure
        # would name a step that had in fact already succeeded.
        try:
            os.fchmod(fd, _FILE_MODE)
            with _translated_deadline():
                acquire_exclusive_lock(fd, deadline=deadline, step=f"{request.step_prefix}:lock")
        except OSError as exc:
            raise _fail(PUBLICATION_FAILED, f"cannot lock {request.target_root}: {exc}") from exc
        yield
    finally:
        _release_descriptor(fd)


def _replace_temporary(dir_fd: int, request: PublicationRequest) -> None:
    try:
        os.unlink(request.temporary_name, dir_fd=dir_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _fail(
            PUBLICATION_FAILED,
            f"cannot clear the {request.noun} temporary below {request.target_root}: {exc}",
        ) from exc


def _write_all(file_fd: int, request: PublicationRequest, deadline: Deadline) -> None:
    """Write the payload in bounded chunks, checking the ceiling between each one."""

    payload = request.payload
    written = 0
    while written < len(payload):
        with _translated_deadline():
            deadline.check(f"{request.step_prefix}:write")
        try:
            written += os.write(file_fd, payload[written : written + _WRITE_CHUNK_BYTES])
        except OSError as exc:
            raise _fail(
                PUBLICATION_FAILED, f"cannot write the {request.noun} below {request.target_root}: {exc}"
            ) from exc


def _open_target_directory(request: PublicationRequest) -> int:
    """Open the target directory, creating and reopening every component with O_NOFOLLOW."""

    if not request.target_root.is_relative_to(request.owner_root):
        raise _fail(
            PUBLICATION_FAILED,
            f"the publication target {request.target_root} must be the declared owner root "
            f"{request.owner_root} or a path below it",
        )
    relative = request.target_root.relative_to(request.owner_root)
    try:
        dir_fd = os.open(request.owner_root, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise _fail(PUBLICATION_FAILED, f"cannot open {request.owner_root}: {exc}") from exc
    try:
        for part in relative.parts:
            child = _open_owned_child(dir_fd, part, request)
            # The child is already open; releasing its parent decides nothing.
            _release_descriptor(dir_fd)
            dir_fd = child
    except BaseException:
        _release_descriptor(dir_fd)
        raise
    return dir_fd


def _open_owned_child(parent_fd: int, name: str, request: PublicationRequest) -> int:
    try:
        os.mkdir(name, _DIRECTORY_MODE, dir_fd=parent_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise _fail(PUBLICATION_FAILED, f"cannot create artifact component {name!r}: {exc}") from exc
    try:
        child = os.open(name, _NOFOLLOW_DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise _fail(
            PUBLICATION_FAILED,
            f"artifact component {name!r} must be an evaluation-owned directory: {exc}",
        ) from exc
    # ``mkdir`` is masked by the ambient umask; a service-owned directory is always 0700.
    try:
        os.fchmod(child, _DIRECTORY_MODE)
    except OSError as exc:
        _release_descriptor(child)
        raise _fail(
            PUBLICATION_FAILED,
            f"cannot own artifact component {name!r} below {request.target_root}: {exc}",
        ) from exc
    return child
