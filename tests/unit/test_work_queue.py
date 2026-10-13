"""Bounded queue backpressure and identity tracking."""

import asyncio

import pytest

from finbot_ingestion.ingestion.work_queue import WorkQueue

from tests.support.scheduling import FakeClock


def test_queue_backpressure_cancellation_keeps_inflight_identity():
    async def scenario():
        q = WorkQueue(1, FakeClock())
        assert q.offer("a", "a")
        put = asyncio.create_task(q.put("b", "b"))
        await asyncio.sleep(0)
        put.cancel()
        with pytest.raises(asyncio.CancelledError):
            await put
        assert not q.contains("b")
        item = await q.get()
        assert not q.offer("a", "duplicate")
        assert q.offer("b", "b")
        q.done(item)
        q.discard()
        await q.queue.join()
        assert not q.keys
    asyncio.run(scenario())
