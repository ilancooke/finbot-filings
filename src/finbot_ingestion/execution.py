"""Bounded blocking I/O admission and explicit lifecycle for async services."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial


class BlockingExecution:
    def __init__(self, *, max_workers=1, executor=None, name="ingestion"):
        if type(max_workers) is not int or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        self.max_workers = max_workers
        self.executor = executor if executor is not None else ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=name)
        self._owns_executor = executor is None
        self._closed = False
        self._slots = None
        self._active = 0

    async def call(self, operation, **params):
        if self._closed:
            raise RuntimeError("execution is closed")
        if self._slots is None:
            self._slots = asyncio.Semaphore(self.max_workers)
        async with self._slots:
            if self._closed:
                raise RuntimeError("execution is closed")
            future = asyncio.get_running_loop().run_in_executor(
                self.executor, partial(operation, **params))
            self._active += 1
            cancelled = False
            try:
                # Repeated cancellation must not release admission before I/O ends.
                while True:
                    try:
                        result = await asyncio.shield(future)
                        break
                    except asyncio.CancelledError:
                        if future.cancelled():
                            raise
                        cancelled = True
                    except Exception:
                        if cancelled:
                            raise asyncio.CancelledError from None
                        raise
                if cancelled:
                    raise asyncio.CancelledError
                return result
            finally:
                self._active -= 1

    def close(self):
        if self._active:
            raise RuntimeError("await in-flight work before closing execution")
        if self._closed:
            return
        self._closed = True
        if self._owns_executor:
            self.executor.shutdown(wait=True)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
