"""Reusable offline calendar test support."""

from collections import deque
from dataclasses import replace
from datetime import timedelta

from finbot_ingestion.calendar import CalendarConfig, CalendarSnapshot
from finbot_ingestion.calendar.service import CalendarSyncService
from finbot_ingestion.domain import Company, ExpectedEarningsEvent

from tests.support.dynamodb import NOW


APPLE = Company("AAPL", "320193", "Apple")


def event(**changes):
    return replace(ExpectedEarningsEvent(company_cik=APPLE.cik, ticker=APPLE.ticker, expected_date=NOW.date(),
        synced_at=NOW - timedelta(days=1), provider="fixture", time_of_day="before_market", provider_event_id="apple-q3"), **changes)


class FakeProvider:
    name = "fixture"

    def __init__(self, events=()):
        self.events = tuple(events)
        self.calls, self.failures, self.changes = [], deque(), {}

    async def fetch_events(self, start_date, end_date, companies):
        self.calls.append((start_date, end_date, tuple(companies)))
        if self.failures:
            raise self.failures.popleft()
        snapshot = CalendarSnapshot(provider=self.name, start_date=start_date, end_date=end_date,
            company_ciks=tuple(company.cik for company in companies), events=self.events, complete=True)
        return replace(snapshot, **self.changes)


async def no_sleep(seconds):
    pass


def service(db, provider, **config):
    return CalendarSyncService(provider, db.companies, db.calendar, CalendarConfig(provider="fixture", **config),
                               now=lambda: NOW, sleeper=no_sleep, random_value=lambda: 0)


async def seed(db, events=()):
    await db.companies.upsert(APPLE)
    await db.calendar.upsert_events(events)
