"""Offline exchange sessions; no provider requests or handwritten holidays."""

from typing import Protocol
from datetime import date, datetime


class MarketSessions(Protocol):
    def session(self, day: date) -> tuple[datetime, datetime] | None: ...


class ExchangeMarketSessions:
    def __init__(self, *, start: date, end: date, name="XNYS"):
        import exchange_calendars
        self.start, self.end = start, end
        self.calendar = exchange_calendars.get_calendar(name, start=start, end=end)

    def session(self, day):
        if not self.start <= day <= self.end:
            raise ValueError("market session date outside initialized coverage")
        label = day.isoformat()
        if not self.calendar.is_session(label):
            return None
        return (self.calendar.session_open(label).to_pydatetime(),
                self.calendar.session_close(label).to_pydatetime())
