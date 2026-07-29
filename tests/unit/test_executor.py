from __future__ import annotations

import threading

import pytest

from serena_light.lsp.executor import BoundedLspExecutor, ExecutorBusyError


def test_cleanup_reserve_is_independent_of_the_ordinary_queue_bound() -> None:
    executor = BoundedLspExecutor(queue_capacity=1, name="cleanup-reserve")
    entered = threading.Event()
    release = threading.Event()
    order: list[str] = []
    try:
        def block_worker() -> None:
            entered.set()
            assert release.wait(5)

        active = executor.submit(block_worker)
        assert entered.wait(5)
        ordinary = executor.submit(lambda: order.append("ordinary"))
        with pytest.raises(ExecutorBusyError):
            executor.submit(lambda: None)

        cleanup_python = executor._submit_cleanup(lambda: order.append("python-cleanup"))
        cleanup_typescript = executor._submit_cleanup(lambda: order.append("typescript-cleanup"))
        with pytest.raises(ExecutorBusyError):
            executor._submit_cleanup(lambda: order.append("overflow-cleanup"))
        assert executor.snapshot().queue_size == 1
        release.set()

        active.result(timeout=5)
        ordinary.result(timeout=5)
        cleanup_python.result(timeout=5)
        cleanup_typescript.result(timeout=5)
        assert order == ["ordinary", "python-cleanup", "typescript-cleanup"]
    finally:
        release.set()
        executor.close()
