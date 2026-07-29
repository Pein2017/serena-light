from __future__ import annotations

import asyncio
import threading

from serena_light.lsp.executor import BoundedLspExecutor


def test_blocking_workspace_does_not_block_event_loop_status_heartbeats_or_another_root() -> None:
    async def scenario() -> None:
        first_root = BoundedLspExecutor(queue_capacity=2, name="first-root")
        second_root = BoundedLspExecutor(queue_capacity=2, name="second-root")
        started = threading.Event()
        release = threading.Event()
        heartbeat_count = 0
        stop_heartbeats = asyncio.Event()

        def blocking_request() -> str:
            started.set()
            assert release.wait(1)
            return "first-done"

        async def heartbeats() -> None:
            nonlocal heartbeat_count
            while not stop_heartbeats.is_set():
                heartbeat_count += 1
                await asyncio.sleep(0.01)

        heartbeat_task = asyncio.create_task(heartbeats())
        first_future = first_root.submit(blocking_request)
        try:
            assert await asyncio.to_thread(started.wait, 0.5)
            assert first_root.snapshot().active is True
            assert first_root.snapshot().queue_capacity == 2
            assert await asyncio.wait_for(asyncio.wrap_future(second_root.submit(lambda: "second-done")), 0.2) == (
                "second-done"
            )
            await asyncio.sleep(0.05)
            assert heartbeat_count >= 3
            release.set()
            assert await asyncio.wait_for(asyncio.wrap_future(first_future), 0.5) == "first-done"
        finally:
            release.set()
            stop_heartbeats.set()
            await heartbeat_task
            await asyncio.to_thread(first_root.close)
            await asyncio.to_thread(second_root.close)

    asyncio.run(scenario())
