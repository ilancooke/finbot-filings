"""Bounded FIFO admission with identity suppression through workflow completion."""

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QueuedWork:
    key: str
    payload: object
    queued_at: float


class WorkQueue:
    def __init__(self, capacity, clock):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("positive queue capacity required")
        self.queue = asyncio.Queue(maxsize=capacity)
        self.clock, self.keys, self.waiting = clock, set(), {}
        self.accepting = True

    def contains(self, key):
        return key in self.keys

    def offer(self, key, payload):
        if not self.accepting or key in self.keys or self.queue.full():
            return False
        item = QueuedWork(key, payload, self.clock.monotonic())
        self.keys.add(key)
        self.waiting[key] = item.queued_at
        self.queue.put_nowait(item)
        return True

    async def put(self, key, payload):
        if not self.accepting or key in self.keys:
            return False
        self.keys.add(key)
        try:
            item = QueuedWork(key, payload, self.clock.monotonic())
            await self.queue.put(item)
            self.waiting[key] = item.queued_at
            return True
        except BaseException:
            self.keys.discard(key)
            raise

    async def get(self):
        item = await self.queue.get()
        self.waiting.pop(item.key, None)
        return item

    def done(self, item):
        self.keys.remove(item.key)
        self.queue.task_done()

    def snapshot(self):
        oldest = min(self.waiting.values(), default=self.clock.monotonic())
        return {"depth": self.queue.qsize(), "identities": len(self.keys),
                "oldest_age_seconds": max(0, self.clock.monotonic() - oldest)}

    def discard(self):
        self.accepting = False
        while not self.queue.empty():
            self.done(self.queue.get_nowait())
        self.waiting.clear()
