"""Reusable offline yahoo sync test support."""

from contextlib import asynccontextmanager
from datetime import timedelta

import pytest

from finbot_ingestion.calendar import CalendarConfig
from finbot_ingestion.calendar.providers.yahoo import YahooEarningsCalendarProvider
from finbot_ingestion.calendar.providers.yahoo_client import FetchBudget
from finbot_ingestion.calendar.providers.yahoo_config import YahooConfig
from finbot_ingestion.calendar.reconciliation import ReplacementOnlyPolicy
from finbot_ingestion.calendar.service import CalendarSyncService
from finbot_ingestion.domain import Company, ExpectedEarningsEvent
from finbot_ingestion.execution import BlockingExecution

from tests.support.dynamodb import NOW


COMPANY = Company("AAPL", "320193", "Synthetic company")
DAY = NOW.date()


def event(day=DAY, **options):
    return ExpectedEarningsEvent(company_cik=COMPANY.cik, ticker=COMPANY.ticker, expected_date=day,
        synced_at=NOW - timedelta(days=1), provider="yahoo", time_of_day="after_market",
        replacement_hint=options.pop("replacement_hint", "quarterly-announcement-v1/2026/Q3"), **options)


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(FetchBudget, "wait", lambda budget, seconds: budget.remaining())


async def no_sleep(seconds):
    pass


@asynccontextmanager
async def service(db, source, *, now=lambda:NOW):
    with BlockingExecution(name="yahoo-sync-test") as execution:
        provider = YahooEarningsCalendarProvider(YahooConfig(), execution, client=source, now=now)
        calendar = CalendarSyncService(provider, db.companies, db.calendar,
            CalendarConfig(provider="yahoo", provider_attempts=1), now=now, sleeper=no_sleep,
            reconciliation=ReplacementOnlyPolicy(), market_timezone="America/New_York", reconciliation_lookback_days=3)
        try:
            yield calendar
        finally:
            await provider.close()


async def seed(db, events):
    await db.companies.upsert(COMPANY)
    await db.calendar.upsert_events(events)
