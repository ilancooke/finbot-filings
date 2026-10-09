"""Persistence boundary for versioned scheduling satisfaction."""

from typing import Protocol
from finbot_ingestion.domain.satisfaction import EventIdentity, EventSatisfaction


class SatisfactionRepository(Protocol):
    async def get(self, identity: EventIdentity) -> EventSatisfaction | None: ...
    async def create_if_absent(self, satisfaction: EventSatisfaction) -> bool: ...
