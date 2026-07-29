"""Bounded single-worker execution for one workspace adapter."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

T = TypeVar("T")


class ExecutorBusyError(RuntimeError):
    """Raised when a workspace LSP queue has reached its fixed bound."""


class EditCommitState(StrEnum):
    """The forward-only commit states of one non-replayable executor edit."""

    QUEUED = "queued"
    RUNNING = "running"
    INSTALLED = "installed"
    DONE = "done"


_COMMIT_ORDER: tuple[EditCommitState, ...] = (
    EditCommitState.QUEUED,
    EditCommitState.RUNNING,
    EditCommitState.INSTALLED,
    EditCommitState.DONE,
)


class EditCommit:
    """Commit progress shared between one queued edit and its waiting caller.

    The caller reads this after a timeout or a lost response to choose between
    ``TIMED_OUT`` (the work provably never ran, so a later write is impossible)
    and ``UNCERTAIN`` (the work started, or its state cannot be proven).  Only
    forward transitions are accepted, so a stale worker can never demote the
    state a caller has already acted on.
    """

    __slots__ = ("_lock", "_state")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = EditCommitState.QUEUED

    @property
    def state(self) -> EditCommitState:
        with self._lock:
            return self._state

    @property
    def installed(self) -> bool:
        """Whether the replacement provably reached the filesystem."""

        return self.state in {EditCommitState.INSTALLED, EditCommitState.DONE}

    def mark_running(self) -> None:
        self._advance(EditCommitState.RUNNING)

    def mark_installed(self) -> None:
        self._advance(EditCommitState.INSTALLED)

    def mark_done(self) -> None:
        self._advance(EditCommitState.DONE)

    def _advance(self, state: EditCommitState) -> None:
        with self._lock:
            if _COMMIT_ORDER.index(state) <= _COMMIT_ORDER.index(self._state):
                raise ValueError(f"edit commit cannot move from {self._state.value} to {state.value}")
            self._state = state


@dataclass(frozen=True)
class ExecutorSnapshot:
    queue_size: int
    queue_capacity: int
    active: bool
    stopping: bool


@dataclass
class _WorkItem:
    future: Future[Any]
    call: Callable[[], Any]
    cleanup: bool = False


_STOP = object()


class BoundedLspExecutor:
    """Order LSP work without blocking the daemon event loop."""

    # A workspace owns at most one adapter per supported language family.  Keep
    # their cleanup admission separate from the ordinary work bound so a full
    # request queue cannot disown the processes whose termination it requires.
    _CLEANUP_RESERVE = 2

    def __init__(self, *, queue_capacity: int = 32, name: str = "workspace") -> None:
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be positive")
        self._queue: queue.Queue[_WorkItem | object] = queue.Queue(
            maxsize=queue_capacity + self._CLEANUP_RESERVE
        )
        self._capacity = queue_capacity
        self._state_lock = threading.Lock()
        self._ordinary_queued = 0
        self._cleanup_queued = 0
        self._active = False
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name=f"serena-light-lsp:{name}", daemon=False)
        self._thread.start()

    def submit(self, call: Callable[[], T]) -> Future[T]:
        future: Future[T] = Future()
        with self._state_lock:
            if self._stopping:
                raise RuntimeError("LSP executor is stopping")
            if self._ordinary_queued >= self._capacity:
                raise ExecutorBusyError(f"LSP executor queue is full ({self._capacity})")
            try:
                self._queue.put_nowait(_WorkItem(future=future, call=call))
            except queue.Full as exc:
                raise ExecutorBusyError(f"LSP executor queue is full ({self._capacity})") from exc
            self._ordinary_queued += 1
        return future

    def _submit_cleanup(self, call: Callable[[], T]) -> Future[T]:
        """Use the bounded cleanup reserve without consuming ordinary capacity."""

        future: Future[T] = Future()
        with self._state_lock:
            if self._stopping:
                raise RuntimeError("LSP executor is stopping")
            if self._cleanup_queued >= self._CLEANUP_RESERVE:
                raise ExecutorBusyError("LSP executor cleanup reserve is full")
            try:
                self._queue.put_nowait(_WorkItem(future=future, call=call, cleanup=True))
            except queue.Full as exc:
                raise ExecutorBusyError("LSP executor cleanup reserve is full") from exc
            self._cleanup_queued += 1
        return future

    def snapshot(self) -> ExecutorSnapshot:
        with self._state_lock:
            return ExecutorSnapshot(
                queue_size=self._ordinary_queued,
                queue_capacity=self._capacity,
                active=self._active,
                stopping=self._stopping,
            )

    def close(self, *, cancel_queued: bool = True, timeout: float = 5.0) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        with self._state_lock:
            if self._stopping:
                already_stopping = True
            else:
                self._stopping = True
                already_stopping = False
        if not already_stopping:
            if cancel_queued:
                self._cancel_queued()
            self._queue.put(_STOP, timeout=timeout)
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            raise TimeoutError("LSP executor did not reach its bounded terminal state")

    def _cancel_queued(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                if isinstance(item, _WorkItem):
                    with self._state_lock:
                        if item.cleanup:
                            self._cleanup_queued -= 1
                        else:
                            self._ordinary_queued -= 1
                    item.future.cancel()
            finally:
                self._queue.task_done()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                assert isinstance(item, _WorkItem)
                with self._state_lock:
                    if item.cleanup:
                        self._cleanup_queued -= 1
                    else:
                        self._ordinary_queued -= 1
                if not item.future.set_running_or_notify_cancel():
                    continue
                with self._state_lock:
                    self._active = True
                try:
                    result = item.call()
                except BaseException as exc:
                    item.future.set_exception(exc)
                else:
                    item.future.set_result(result)
                finally:
                    with self._state_lock:
                        self._active = False
            finally:
                self._queue.task_done()
