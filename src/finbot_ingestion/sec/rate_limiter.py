"""Shared, conservative pacing without idle-time burst credit."""
import math
import threading
import time
from collections import deque

class SECRateLimiter:
    def __init__(self, requests_per_second=5.0, *, clock=time.monotonic, sleeper=time.sleep):
        if isinstance(requests_per_second, bool) or not math.isfinite(requests_per_second) or not 0 < requests_per_second <= 5:
            raise ValueError("SEC rate must be finite, positive, and <= 5")
        self.rate = requests_per_second
        self.clock, self.sleep = clock, sleeper
        self._starts = deque()
        self._last = None
        # Shared by clients using this budget; held through transport dispatch/response.
        self.dispatch_lock = threading.Lock()

    def wait(self):
        """Called only while dispatch_lock is held, immediately before transport."""
        while True:
            now = self.clock()
            while self._starts and now - self._starts[0] > 1.0:
                self._starts.popleft()
            delay = 0.0 if self._last is None else self._last + 1 / self.rate - now
            if len(self._starts) >= 5:
                delay = max(delay, self._starts[0] + 1.000000001 - now)
            if delay > 0:
                self.sleep(delay)
                continue
            self._last = now
            self._starts.append(now)
            return
