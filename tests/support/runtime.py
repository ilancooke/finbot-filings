"""Reusable offline runtime test support."""

from datetime import date
import asyncio
import time

from finbot_ingestion.calendar import CalendarConfig
from finbot_ingestion.calendar.service import CalendarSyncService
from finbot_ingestion.domain.sec_items import SECItemMetadata, FilingObservation
from finbot_ingestion.ingestion.recovery_service import RecoveryService
from finbot_ingestion.observability.metrics import Metrics
from finbot_ingestion.repositories.dynamodb.satisfaction import DynamoDBSatisfactionRepository
from finbot_ingestion.runtime.application import RuntimeApplication
from finbot_ingestion.runtime.config import RuntimeConfig
from finbot_ingestion.scheduler.market_sessions import ExchangeMarketSessions
from finbot_ingestion.scheduler.window_policy import WindowPolicy

from tests.support.calendar import FakeProvider, event, APPLE
from tests.support.dynamodb import make_filing, NOW


class TestClock:
    __test__ = False

    def now(self):
        return NOW

    def monotonic(self):
        return time.monotonic()

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


async def until(predicate, *, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.005)


async def setup(system, tmp_path, *, status="known", items=("2.02",), form="8-K", provider="fixture"):
    system.sec.filings = [make_filing(form_type=form)]
    def evidence(company):
        system.sec._call("submissions")
        return tuple(FilingObservation(f, SECItemMetadata(f.accession_number, f.company_cik, status, items))
                     for f in system.sec.filings)
    system.sec.get_company_submissions_with_evidence = evidence
    await system.db.companies.upsert(APPLE)
    expected = event(time_of_day="after_market", provider=provider)
    await system.db.calendar.upsert_events([expected])
    config = RuntimeConfig(tick_seconds=.005, active_poll_seconds=.01, safety_poll_seconds=1,
        metrics_flush_seconds=.005, heartbeat_seconds=.005, recovery_seconds=100, reload_seconds=100,
        refresh_retry_seconds=100, shutdown_grace_seconds=.1, health_path=str(tmp_path / "health.json"))
    service = (CalendarSyncService(FakeProvider([expected]), system.db.companies, system.db.calendar,
               CalendarConfig(provider="fixture"), now=lambda: NOW) if provider == "fixture" else
               CalendarSyncService.with_placeholder(system.db.companies, system.db.calendar, now=lambda: NOW))
    worker = system.worker()
    app = RuntimeApplication(config=config, calendar_service=service,
        satisfaction=DynamoDBSatisfactionRepository(system.db.execution, system.db.filings), worker=worker,
        recovery=RecoveryService(worker), clock=TestClock(), metrics=Metrics(sink=lambda _: None),
        windows=WindowPolicy(ExchangeMarketSessions(start=date(2026, 1, 1), end=date(2026, 12, 31)), config))
    return app, expected
