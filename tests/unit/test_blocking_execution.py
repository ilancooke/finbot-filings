"""Cancellation and lifecycle of bounded blocking execution."""

import asyncio
import threading

import pytest

from finbot_ingestion.execution import BlockingExecution


def test_repeated_cancellation_retains_admission_until_blocking_call_finishes():
    async def scenario():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        completed = threading.Event()
        def call():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2)
            completed.set()
        with BlockingExecution(max_workers=1) as execution:
            task = asyncio.create_task(execution.call(call))
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            with pytest.raises(RuntimeError, match="in-flight"):
                execution.close()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert completed.is_set()
            assert await execution.call(lambda: "next") == "next"
    asyncio.run(scenario())
