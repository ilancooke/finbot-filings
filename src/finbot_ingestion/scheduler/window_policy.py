"""One market-local window policy, represented as half-open UTC intervals."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
import math
from zoneinfo import ZoneInfo

from finbot_ingestion.domain.validation import utc_datetime


@dataclass(frozen=True, slots=True)
class EarningsWindow:
    start: datetime
    end: datetime
    grace_end: datetime

    def __post_init__(self):
        for name in ("start", "end", "grace_end"):
            object.__setattr__(self, name, utc_datetime(getattr(self, name), name))
        if not self.start < self.end <= self.grace_end:
            raise ValueError("invalid earnings window")

    def contains(self, at):
        return self.start <= utc_datetime(at, "at") < self.grace_end


class WindowPolicy:
    def __init__(self, sessions, config):
        self.sessions, self.config = sessions, config
        self.zone = ZoneInfo(config.market_timezone)
        self._cache = {}

    @property
    def lookback_days(self):
        c = self.config
        return 2 + math.ceil((max(c.after_close_seconds, c.after_open_seconds) + c.grace_seconds) / 86400)

    @property
    def forward_days(self):
        return 1 + math.ceil(max(self.config.before_open_seconds, self.config.before_close_seconds) / 86400)

    def window(self, event):
        key = (event.expected_date, event.time_of_day)
        if key in self._cache:
            return self._cache[key]
        c = self.config
        session = self.sessions.session(event.expected_date)
        if session is None:
            start = datetime.combine(event.expected_date, time(c.non_session_start_hour,
                c.non_session_start_minute), self.zone)
            end = datetime.combine(event.expected_date, time(c.non_session_end_hour), self.zone)
        else:
            opened, closed = session
            opened, closed = utc_datetime(opened, "open"), utc_datetime(closed, "close")
            if event.time_of_day == "before_market":
                start, end = opened - timedelta(seconds=c.before_open_seconds), opened + timedelta(seconds=c.after_open_seconds)
            elif event.time_of_day == "after_market":
                start, end = closed - timedelta(seconds=c.before_close_seconds), closed + timedelta(seconds=c.after_close_seconds)
            elif event.time_of_day in (None, "unknown"):
                start, end = opened - timedelta(seconds=c.before_open_seconds), closed + timedelta(seconds=c.after_close_seconds)
            else:
                raise ValueError("unnormalized calendar time")
        start, end = utc_datetime(start, "start"), utc_datetime(end, "end")
        result = EarningsWindow(start, end, end + timedelta(seconds=c.grace_seconds))
        if len(self._cache) >= 4096:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = result
        return result
