"""A complete snapshot is authoritative only for its declared date/company scope."""

from collections.abc import Sequence
from datetime import date
from typing import Protocol

from finbot_ingestion.domain import Company
from .contracts import CalendarSnapshot


class CalendarProviderError(RuntimeError):
    pass


class CalendarProviderNotConfigured(CalendarProviderError):
    pass


class CalendarTransientError(CalendarProviderError):
    """Provider adapter may opt into bounded retry for a transient failure."""


class CalendarDataError(CalendarProviderError):
    pass


class IncompleteCalendarSnapshot(CalendarDataError):
    pass


class EarningsCalendarProvider(Protocol):
    name: str

    async def fetch_events(self, start_date: date, end_date: date,
                           companies: Sequence[Company]) -> CalendarSnapshot: ...
