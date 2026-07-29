from __future__ import annotations

import threading

import pytest

from serena_light.lsp.executor import BoundedLspExecutor, EditCommit, EditCommitState, ExecutorBusyError


def test_single_worker_preserves_order() -> None:
    executor = BoundedLspExecutor(queue_capacity=3, name="order")
    observed: list[int] = []
    futures = [executor.submit(lambda value=value: observed.append(value) or value) for value in range(3)]

    assert [future.result(timeout=1) for future in futures] == [0, 1, 2]
    assert observed == [0, 1, 2]
    executor.close()


def test_queue_is_bounded_while_worker_is_blocked() -> None:
    executor = BoundedLspExecutor(queue_capacity=1, name="bounded")
    started = threading.Event()
    release = threading.Event()
    first = executor.submit(lambda: started.set() or release.wait(1))
    assert started.wait(1)
    queued = executor.submit(lambda: "queued")

    with pytest.raises(ExecutorBusyError, match="queue is full"):
        executor.submit(lambda: "overflow")

    release.set()
    assert first.result(timeout=1) is True
    assert queued.result(timeout=1) == "queued"
    executor.close()


def test_cancelled_queued_work_never_starts() -> None:
    executor = BoundedLspExecutor(queue_capacity=2, name="cancel")
    started = threading.Event()
    release = threading.Event()
    ran_cancelled = threading.Event()
    active = executor.submit(lambda: started.set() or release.wait(1))
    assert started.wait(1)
    queued = executor.submit(lambda: ran_cancelled.set())

    assert queued.cancel() is True
    release.set()
    assert active.result(timeout=1) is True
    executor.close()
    assert queued.cancelled()
    assert not ran_cancelled.is_set()


def test_started_work_reaches_bounded_terminal_state_and_does_not_hold_external_lock() -> None:
    executor = BoundedLspExecutor(queue_capacity=1, name="started")
    workspace_lock = threading.Lock()
    started = threading.Event()
    release = threading.Event()

    def blocking_call() -> str:
        with workspace_lock:
            started.set()
            assert release.wait(1)
        return "done"

    future = executor.submit(blocking_call)
    assert started.wait(1)
    assert future.cancel() is False
    release.set()
    assert future.result(timeout=1) == "done"
    assert workspace_lock.acquire(timeout=0.2)
    workspace_lock.release()
    executor.close()


def test_close_cancels_queued_work_but_waits_for_started_work() -> None:
    executor = BoundedLspExecutor(queue_capacity=2, name="close")
    started = threading.Event()
    release = threading.Event()
    active = executor.submit(lambda: started.set() or release.wait(1))
    assert started.wait(1)
    queued = executor.submit(lambda: "never")
    closer = threading.Thread(target=executor.close, kwargs={"timeout": 1})
    closer.start()
    release.set()
    closer.join(timeout=1)

    assert not closer.is_alive()
    assert active.result(timeout=1) is True
    assert queued.cancelled()


def test_edit_commit_states_move_forward_only() -> None:
    commit = EditCommit()

    assert commit.state is EditCommitState.QUEUED
    assert not commit.installed
    commit.mark_running()
    commit.mark_installed()
    assert commit.installed
    commit.mark_done()
    assert commit.state is EditCommitState.DONE

    with pytest.raises(ValueError, match="cannot move from done to running"):
        commit.mark_running()
    with pytest.raises(ValueError, match="cannot move from done to done"):
        commit.mark_done()


def test_proven_queued_cancellation_leaves_the_commit_unable_to_write() -> None:
    executor = BoundedLspExecutor(queue_capacity=2, name="commit")
    commit = EditCommit()
    started = threading.Event()
    release = threading.Event()
    wrote = threading.Event()
    active = executor.submit(lambda: started.set() or release.wait(1))
    assert started.wait(1)

    def edit() -> str:
        commit.mark_running()
        wrote.set()
        return "installed"

    queued = executor.submit(edit)

    # Cancellation succeeds only while the entry is still queued, which is the
    # proof that a later write is impossible.
    assert queued.cancel() is True
    assert commit.state is EditCommitState.QUEUED
    release.set()
    assert active.result(timeout=1) is True
    executor.close()

    assert queued.cancelled()
    assert not wrote.is_set()
    assert commit.state is EditCommitState.QUEUED


def test_started_edit_cannot_be_cancelled_and_reports_a_running_commit() -> None:
    executor = BoundedLspExecutor(queue_capacity=1, name="running")
    commit = EditCommit()
    started = threading.Event()
    release = threading.Event()

    def edit() -> str:
        commit.mark_running()
        started.set()
        assert release.wait(1)
        return "done"

    future = executor.submit(edit)
    assert started.wait(1)

    assert future.cancel() is False
    assert commit.state is EditCommitState.RUNNING
    assert not commit.installed
    release.set()
    assert future.result(timeout=1) == "done"
    executor.close()
