"""Bounded SDK execution, reusable injected client, and explicit lifecycle."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from botocore.config import Config

from .config import DynamoDBConfig


class DynamoDBExecution:
    def __init__(self, client, config: DynamoDBConfig, *, executor=None):
        self.client, self.config = client, config
        self.executor = executor or ThreadPoolExecutor(max_workers=config.max_workers, thread_name_prefix="dynamodb")
        self._owns_executor = executor is None
        self._closed = False
        self._slots = None

    @classmethod
    def from_config(cls, config: DynamoDBConfig, *, session=None):
        """Explicit opt-in client construction; never invoked on import or parsing."""
        import boto3

        session = session or boto3.Session(region_name=config.region)
        client = session.client("dynamodb", region_name=config.region, config=Config(
            connect_timeout=config.connect_timeout_seconds,
            read_timeout=config.read_timeout_seconds,
            retries={"mode": "standard", "total_max_attempts": config.max_attempts},
            max_pool_connections=config.max_workers,
        ))
        return cls(client, config)

    async def call(self, operation, **params):
        if self._closed:
            raise RuntimeError("DynamoDB execution is closed")
        if self._slots is None:
            self._slots = asyncio.Semaphore(self.config.max_workers)
        async with self._slots:
            # Keep admission until the actual SDK call ends, even on cancellation.
            future = asyncio.get_running_loop().run_in_executor(
                self.executor, partial(operation, **params))
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                await asyncio.shield(future)
                raise

    def close(self):
        self._closed = True
        if self._owns_executor:
            self.executor.shutdown(wait=True)
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
