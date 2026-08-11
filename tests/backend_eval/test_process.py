"""The shared monotonic deadline and the bounded, process-group-killing runner."""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import pytest

from scripts.backend_eval.process import (
    CommandTimeout,
    Deadline,
    DeadlineExceeded,
    run_bounded_bytes,
    subprocess_runner,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- the deadline ----------------------------------------------------------------


def test_deadline_reserves_finalization_time_for_the_collecting_phase() -> None:
    clock = _Clock()
    deadline = Deadline.start(clock, 100, reserve=30)
    assert deadline.remaining() == pytest.approx(70.0)
    clock.advance(70.0)
    assert deadline.expired()
    with pytest.raises(DeadlineExceeded, match="collect"):
        deadline.check("collect")
    finalization = deadline.finalization()
    assert not finalization.expired()
    assert finalization.remaining() == pytest.approx(30.0)
    finalization.check("publish")


def test_deadline_finalization_still_expires_at_the_whole_ceiling() -> None:
    clock = _Clock()
    deadline = Deadline.start(clock, 100, reserve=30).finalization()
    clock.advance(100.0)
    with pytest.raises(DeadlineExceeded, match="artifact_digest") as error:
        deadline.check("artifact_digest")
    assert "budget=100" in str(error.value)


def test_deadline_remaining_never_goes_negative() -> None:
    clock = _Clock()
    deadline = Deadline.start(clock, 10)
    clock.advance(50.0)
    assert deadline.remaining() == 0.0


# --- the bounded runner ------------------------------------------------------------


def test_subprocess_runner_returns_the_completed_result(tmp_path: Path) -> None:
    result = subprocess_runner(
        ["/bin/sh", "-c", "printf out; printf err 1>&2; exit 3"], cwd=tmp_path, env={}, timeout=30.0
    )
    assert (result.returncode, result.stdout, result.stderr) == (3, "out", "err")


def test_subprocess_runner_refuses_to_start_without_remaining_time(tmp_path: Path) -> None:
    with pytest.raises(CommandTimeout, match="no remaining time"):
        subprocess_runner(["/bin/sh", "-c", "exit 0"], cwd=tmp_path, env={}, timeout=0.0)


def test_subprocess_runner_kills_the_whole_process_group_of_a_hung_command(tmp_path: Path) -> None:
    """A real hung command with a real orphan-making child must not outlive the call."""

    marker = tmp_path / "child.pid"
    script = f"sleep 600 & echo $! > {marker}; sleep 600"
    started = time.monotonic()
    with pytest.raises(CommandTimeout, match="timed out"):
        subprocess_runner(["/bin/sh", "-c", script], cwd=tmp_path, env={}, timeout=1.0)
    assert time.monotonic() - started < 25.0
    child_pid = int(marker.read_text().strip())
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"the background child {child_pid} survived the killed process group")


def test_run_bounded_bytes_returns_raw_output_and_bounds_it(tmp_path: Path) -> None:
    result = run_bounded_bytes(["/bin/sh", "-c", "printf 'a\\0b\\0'"], cwd=tmp_path, env={}, timeout=30.0)
    assert result.returncode == 0
    assert result.stdout == b"a\0b\0"
    with pytest.raises(CommandTimeout):
        run_bounded_bytes(["/bin/sh", "-c", "sleep 600"], cwd=tmp_path, env={}, timeout=1.0)


def test_subprocess_runner_starts_a_new_session_so_signals_stay_contained(tmp_path: Path) -> None:
    result = subprocess_runner(
        ["/bin/sh", "-c", "echo $$; echo $(ps -o pgid= -p $$)"], cwd=tmp_path, env={}, timeout=30.0
    )
    pid, pgid = (int(value) for value in result.stdout.split())
    assert pid == pgid
    assert pgid != os.getpgid(0)
    assert signal.SIGKILL is not None
