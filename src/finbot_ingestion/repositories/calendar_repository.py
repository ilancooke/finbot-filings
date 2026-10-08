from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from finbot_ingestion.domain import ExpectedEarningsEvent


class CalendarRepository(Protocol):
    async def upsert_events(self, events: Sequence[ExpectedEarningsEvent]) -> None: ...
    async def get_events(self, start: datetime, end: datetime) -> list[ExpectedEarningsEvent]: ...
