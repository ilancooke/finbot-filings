"""Wall time for eligibility, monotonic time for durations and waits."""

import asyncio
from datetime import datetime, timezone
import time


class Clock:
    def now(self):
        return datetime.now(timezone.utc)

    def monotonic(self):
        return time.monotonic()

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)
