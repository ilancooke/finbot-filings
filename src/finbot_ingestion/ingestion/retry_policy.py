"""Bounded, injectable retry timing shared by SEC operations."""
from dataclasses import dataclass
import random
import math

@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_seconds: float = 1.0
    cap_seconds: float = 30.0

    def __post_init__(self):
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in (self.base_seconds, self.cap_seconds)) or self.cap_seconds < self.base_seconds:
            raise ValueError("backoff must be finite, positive, and cap >= base")

    def delay(self, attempt, *, random_value=random.random, retry_after=None):
        ceiling = min(self.cap_seconds, self.base_seconds * 2 ** min(attempt - 1, 30))
        delay = ceiling * random_value()
        if retry_after is not None:
            delay = max(delay, min(self.cap_seconds, max(0, retry_after)))
        return delay
