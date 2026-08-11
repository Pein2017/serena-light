"""One bounded process seam and one monotonic deadline for the whole evaluation.

Every external command the evaluation runs -- ``uv``, ``git``, an interpreter version
probe -- goes through :func:`subprocess_runner` or :func:`run_bounded_bytes`, which start
the child in its own session and, on expiry, ``SIGKILL`` the whole process group before
returning.  A hung ``uv`` or ``git`` therefore cannot outlive the phase that started it,
and cannot keep a pipe open long enough to block cleanup.

:class:`Deadline` is the single monotonic ceiling.  ``reserve`` splits one budget into a
collecting window and a finalization window: the collecting phase stops early enough that
the run can still publish a trustworthy timeout receipt, while :meth:`Deadline.finalization`
keeps the same origin and the same absolute ceiling with no reserve.  Nothing in this module
sleeps, retries, or extends a budget.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

__all__ = [
    "Clock",
    "CommandBytesResult",
    "CommandResult",
    "CommandRunner",
    "CommandTimeout",
    "Deadline",
    "DeadlineExceeded",
    "monotonic_clock",
    "run_bounded_bytes",
    "subprocess_runner",
]

Clock = Callable[[], float]
monotonic_clock: Clock = time.monotonic

# How long a killed process group is given to be reaped before the runner gives up.
_REAP_SECONDS = 20.0


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
    command: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float | None = None
) -> CommandBytesResult:
    """Run one explicit absolute argv, bounded, and keep its output as raw bytes."""

    returncode, stdout, stderr = _run(command, cwd=cwd, env=env, timeout=timeout)
    return CommandBytesResult(returncode=returncode, stdout=stdout, stderr=stderr)


def _run(
    command: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float | None
) -> tuple[int, bytes, bytes]:
    if timeout is not None and timeout <= 0:
        raise CommandTimeout(f"no remaining time to start {command[0]}")
    # Explicit absolute argv, never a shell string.
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Its own session, so the whole group can be killed without touching this process.
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
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
