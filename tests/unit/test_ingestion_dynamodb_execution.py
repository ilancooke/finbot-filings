"""Executor behavior without network, credentials, or AWS resources."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from finbot_ingestion.repositories.dynamodb import DynamoDBConfig, DynamoDBExecution

CONFIG = DynamoDBConfig("us-east-1", "companies", "calendar", "filings", "artifacts", max_workers=1)


def test_client_factory_sets_finite_sdk_policy_and_reuses_session():
    captured = {}
    client = SimpleNamespace(close=lambda: captured.update(closed=True))
    def create(service, **kwargs):
        captured.update(service=service, **kwargs)
        return client
    session = SimpleNamespace(client=create)
    with DynamoDBExecution.from_config(CONFIG, session=session) as execution:
        assert execution.client is client
        assert captured["service"] == "dynamodb"
        policy = captured["config"]
        assert policy.retries == {"mode": "standard", "total_max_attempts": 3}
        assert policy.connect_timeout == 5 and policy.read_timeout == 10
        assert policy.max_pool_connections == 1
    assert captured["closed"]


def test_sdk_calls_do_not_block_event_loop_and_concurrency_is_bounded():
    async def scenario():
        active = 0
        maximum = 0
        lock = threading.Lock()
        loop = asyncio.get_running_loop()
        def operation():
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            released = threading.Event()
            # This cannot run if the synchronous operation blocks the event loop.
            loop.call_soon_threadsafe(released.set)
            assert released.wait(1)
            with lock:
                active -= 1
            return "done"
        with DynamoDBExecution(SimpleNamespace(close=lambda: None), CONFIG) as execution:
            assert await asyncio.gather(*(execution.call(operation) for _ in range(10))) == ["done"] * 10
            assert maximum == 1
        with pytest.raises(RuntimeError, match="closed"):
            await execution.call(operation)
    asyncio.run(scenario())


def test_cancellation_waits_for_sdk_completion_without_losing_admission():
    async def scenario():
        entered = asyncio.Event()
        release = threading.Event()
        completed = threading.Event()
        loop = asyncio.get_running_loop()
        def operation():
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(1)
            completed.set()
        with DynamoDBExecution(SimpleNamespace(close=lambda: None), CONFIG) as execution:
            task = asyncio.create_task(execution.call(operation))
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert completed.is_set()
    asyncio.run(scenario())
