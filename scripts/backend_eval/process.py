"""One bounded process seam and one monotonic deadline for the whole evaluation.

Every external command the evaluation runs -- ``uv``, ``git``, an interpreter version
probe -- goes through :func:`subprocess_runner` or :func:`run_bounded_bytes`, which start
the child in its own session and, on expiry, ``SIGKILL`` the whole process group before
returning.  A hung ``uv`` or ``git`` therefore cannot outlive the phase that started it,
and cannot keep a pipe open long enough to block cleanup.  A child with no request to read
receives ``/dev/null`` on standard input rather than inheriting this process's, so it can
never block waiting for input the phase will never send.

:class:`Deadline` is the single monotonic ceiling.  ``reserve`` splits one budget into a
collecting window and a finalization window: the collecting phase stops early enough that
the run can still publish a trustworthy timeout receipt, while :meth:`Deadline.finalization`
keeps the same origin and the same absolute ceiling with no reserve.  A budget is never
extended here.

:func:`acquire_exclusive_lock` is the one waiting primitive.  A blocking ``flock`` is the
last way a bounded phase can silently exceed its ceiling: it waits on another process for
however long that process holds the lock, outside every deadline check.  Acquisition is
therefore non-blocking and retried against the same monotonic deadline, in the calling
thread, with no helper thread, no signal, and no alarm; a lock that is still held when the
ceiling arrives raises :class:`DeadlineExceeded` like any other expired step.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import signal
import stat
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

__all__ = [
    "GIT_EXECUTABLE",
    "LOCK_POLL_SECONDS",
    "UNBOUNDED_LOCK_WAIT_SECONDS",
    "Clock",
    "CommandBytesResult",
    "CommandResult",
    "CommandRunner",
    "CommandTimeout",
    "Deadline",
    "DeadlineExceeded",
    "ExecutableBindingError",
    "SealedImageError",
    "acquire_exclusive_lock",
    "bound_executable",
    "descriptor_path",
    "monotonic_clock",
    "run_bounded_bytes",
    "sealed_image",
    "subprocess_runner",
]

Clock = Callable[[], float]
monotonic_clock: Clock = time.monotonic
Sleep = Callable[[float], None]

# The one Git the evaluation runs.  It is declared, not discovered: ``shutil.which`` answers
# from whatever ``PATH`` the ambient process happens to carry, which is exactly the kind of
# ambient control every other input of this evaluation refuses.  A missing or non-regular
# Git is a typed failure, never a fallback to a different program.
GIT_EXECUTABLE = Path("/usr/bin/git")

# O_NONBLOCK keeps a FIFO or other blocking special node from hanging the open; the fstat
# regular-file check then refuses it promptly.
_EXECUTABLE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

# How long a killed process group is given to be reaped before the runner gives up.
_REAP_SECONDS = 20.0

# How often a contended lock is retried.  Short enough that a released lock is taken
# promptly, long enough that a long wait costs a negligible number of syscalls.
LOCK_POLL_SECONDS = 0.05

# A caller with no phase ceiling still never waits forever on another process.
UNBOUNDED_LOCK_WAIT_SECONDS = 120.0


# memfd_create and file seals: the conda interpreter's os/fcntl modules omit these constants.
_MFD_CLOEXEC = 0x0001
_MFD_ALLOW_SEALING = 0x0002
_F_ADD_SEALS = 1033
_F_GET_SEALS = 1034
_ALL_SEALS = 0x1 | 0x2 | 0x4 | 0x8


class ExecutableBindingError(RuntimeError):
    """A declared external executable is missing, redirected, or not a regular file."""


class SealedImageError(RuntimeError):
    """An immutable in-memory image of exactly these bytes cannot be provided."""


class DeadlineExceeded(RuntimeError):
    """The monotonic ceiling was reached; the caller must stop with a typed incomplete."""


class CommandTimeout(RuntimeError):
    """A bounded command exceeded its remaining time and its process group was killed."""


@dataclass(frozen=True, slots=True)
class Deadline:
    """A strict monotonic ceiling with an optional reserved finalization window."""

    clock: Clock
    seconds: float
    started: float
    reserve: float = 0.0

    @staticmethod
    def start(clock: Clock, seconds: float, *, reserve: float = 0.0) -> Deadline:
        if seconds <= 0:
            raise ValueError("Deadline.seconds must be positive")
        if reserve < 0 or reserve >= seconds:
            raise ValueError("Deadline.reserve must be non-negative and smaller than the budget")
        return Deadline(clock=clock, seconds=float(seconds), started=clock(), reserve=float(reserve))

    def finalization(self) -> Deadline:
        """The same origin and absolute ceiling, with the reserved window released."""

        return Deadline(clock=self.clock, seconds=self.seconds, started=self.started, reserve=0.0)

    def elapsed(self) -> float:
        return self.clock() - self.started

    def remaining(self) -> float:
        return max(0.0, self.seconds - self.reserve - self.elapsed())

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def check(self, step: str) -> None:
        if self.expired():
            raise DeadlineExceeded(
                f"step={step} elapsed={self.elapsed():.3f}s budget={self.seconds:g}s reserve={self.reserve:g}s"
            )


def acquire_exclusive_lock(
    fd: int,
    *,
    deadline: Deadline | None,
    step: str,
    sleep: Sleep = time.sleep,
    poll_seconds: float = LOCK_POLL_SECONDS,
) -> None:
    """Take an exclusive ``flock`` without ever waiting past the monotonic ceiling.

    A caller that has no deadline still gets a bounded wait: an evaluation step may fail,
    but it may not hang on another process's lock.
    """

    bound = deadline
    if bound is None:
        bound = Deadline.start(monotonic_clock, UNBOUNDED_LOCK_WAIT_SECONDS)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            pass
        remaining = bound.remaining()
        if remaining <= 0.0:
            raise DeadlineExceeded(
                f"step={step} could not acquire an exclusive lock before the ceiling: "
                f"elapsed={bound.elapsed():.3f}s budget={bound.seconds:g}s reserve={bound.reserve:g}s"
            )
        sleep(min(poll_seconds, remaining))


def bound_executable(executable: Path = GIT_EXECUTABLE) -> Path:
    """Prove one declared executable exists as a regular file, through one descriptor.

    This is a *guarded* binding, not a confined one, and the ownership document says so: the
    program lives outside every root this evaluation owns, so there is no parent descriptor to
    walk out from.  What it does close is the ambient-discovery hole -- the argv is one
    declared absolute pathname that is proven to name a regular file before the child starts,
    rather than whatever the ambient ``PATH`` resolves.
    """

    if not executable.is_absolute():
        raise ExecutableBindingError(f"the declared executable must be an absolute path: {executable}")
    try:
        fd = os.open(executable, _EXECUTABLE_FLAGS)
    except OSError as exc:
        raise ExecutableBindingError(f"cannot open the declared executable {executable}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ExecutableBindingError(f"the declared executable must be a regular file: {executable}")
    finally:
        os.close(fd)
    if not os.access(executable, os.X_OK):
        raise ExecutableBindingError(f"the declared executable is not executable: {executable}")
    return executable


def descriptor_path(fd: int) -> Path:
    """The stable pathname of an open descriptor, resolvable by this process and its children."""

    return Path(f"/proc/{os.getpid()}/fd/{fd}")


@contextmanager
def sealed_image(name: str, payload: bytes) -> Iterator[int]:
    """Yield a descriptor on an immutable in-memory image of exactly ``payload``.

    The image is a ``memfd`` sealed ``F_SEAL_WRITE | F_SEAL_SHRINK | F_SEAL_GROW |
    F_SEAL_SEAL`` and read back before it is handed out, so the bytes a child installs or
    executes cannot be changed by anyone -- including transiently -- for the whole call.  It
    is the answer to a mutable pathname: a name can be swapped between the read that bound it
    and the open that used it, an already-sealed descriptor cannot.
    """

    fd = _memfd_create(name)
    try:
        written = 0
        while written < len(payload):
            written += os.write(fd, payload[written:])
        if fcntl.fcntl(fd, _F_ADD_SEALS, _ALL_SEALS) != 0 or fcntl.fcntl(fd, _F_GET_SEALS) != _ALL_SEALS:
            raise SealedImageError(f"cannot seal the {name} image")
        if os.pread(fd, len(payload) + 1, 0) != payload:
            raise SealedImageError(f"the sealed {name} image does not hold the bytes it was built from")
        yield fd
    except OSError as exc:
        raise SealedImageError(f"cannot build the sealed {name} image: {exc}") from exc
    finally:
        os.close(fd)


def _memfd_create(name: str) -> int:
    """Create one sealable anonymous image without starting a process to find libc.

    ``ctypes.util.find_library("c")`` shells out to ``ldconfig``, which would be an unbounded
    child inside a phase whose whole contract is that every child it starts is bounded and
    killable.  The already-loaded process image exports ``memfd_create`` directly, so it is
    resolved from there and no child is started at all.
    """

    handle = ctypes.CDLL(None, use_errno=True)
    if not hasattr(handle, "memfd_create"):
        raise SealedImageError(f"this platform cannot provide a sealed {name} image")
    create = handle.memfd_create
    create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
    create.restype = ctypes.c_int
    fd = create(name.encode("utf-8"), _MFD_CLOEXEC | _MFD_ALLOW_SEALING)
    if fd < 0:
        raise SealedImageError(f"cannot create the sealed {name} image: {os.strerror(ctypes.get_errno())}")
    return fd


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class CommandBytesResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class CommandRunner(Protocol):
    def __call__(
        self, command: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float | None = None
    ) -> CommandResult: ...


def subprocess_runner(
    command: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float | None = None
) -> CommandResult:
    """Run one explicit absolute argv, bounded, and decode its output as text."""

    returncode, stdout, stderr = _run(command, cwd=cwd, env=env, timeout=timeout)
    return CommandResult(
        returncode=returncode,
        stdout=stdout.decode("utf-8", "replace"),
        stderr=stderr.decode("utf-8", "replace"),
    )


def run_bounded_bytes(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float | None = None,
    stdin: bytes | None = None,
    pass_fds: Sequence[int] = (),
) -> CommandBytesResult:
    """Run one explicit absolute argv, bounded, and keep its output as raw bytes.

    ``stdin`` is written to the child and its end closed by ``communicate``, so a child that
    reads its whole request never waits on a writer this process forgot to release.
    ``pass_fds`` keeps exactly those descriptors -- and no others -- open at the same numbers
    in the child, which is how a sealed image is executed instead of a mutable pathname.
    """

    returncode, stdout, stderr = _run(
        command, cwd=cwd, env=env, timeout=timeout, stdin=stdin, pass_fds=pass_fds
    )
    return CommandBytesResult(returncode=returncode, stdout=stdout, stderr=stderr)


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float | None,
    stdin: bytes | None = None,
    pass_fds: Sequence[int] = (),
) -> tuple[int, bytes, bytes]:
    if timeout is not None and timeout <= 0:
        raise CommandTimeout(f"no remaining time to start {command[0]}")
    # Explicit absolute argv, never a shell string.
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=tuple(pass_fds),
        # Its own session, so the whole group can be killed without touching this process.
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        raise CommandTimeout(
            f"{command[0]} timed out after {timeout:g}s and its process group was killed"
        ) from None
    except BaseException:
        _kill_process_group(process)
        raise
    return process.returncode, stdout, stderr


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    """``SIGKILL`` the child's whole group, then reap it and release its pipes."""

    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()
    try:
        process.communicate(timeout=_REAP_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover - a killed group always reaps
        process.kill()
        process.wait(timeout=_REAP_SECONDS)
