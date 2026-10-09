"""Daily cadence, market-date coverage and date-specific satisfaction through restart."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from test_phase4 import system
from test_dynamodb_repositories import db
from test_phase6 import setup, TestClock
from test_yahoo_sync import service, COMPANY, DAY, NOW, no_backoff
from yahoo_fakes import PageSource, row
from finbot_ingestion.calendar.reconciliation import ReplacementOnlyPolicy
from finbot_ingestion.domain.satisfaction import EventIdentity
from finbot_ingestion.domain import Company


class EndReplay(Exception):
    pass


class StepClock:
    def __init__(self, at, limit):
        self.at, self.elapsed, self.limit = at, 0.0, limit

    def now(self):
        return self.at + timedelta(seconds=self.elapsed)

    def monotonic(self):
        return self.elapsed

    async def sleep(self, seconds):
        self.elapsed += seconds
        if self.elapsed > self.limit:
            raise EndReplay()


def wire(app, calendar, clock):
    app.calendar_service, app.clock = calendar, clock
    app.scheduler.clock = clock
    app.config = replace(app.config, tick_seconds=1)


def test_restart_across_market_midnight_does_not_refetch_unexpired_daily_scope(system, tmp_path, no_backoff):
    async def scenario():
        app, _ = await setup(system, tmp_path, provider="yahoo")
        # 23:50 market time; UTC is already the following date.
        observed = datetime(2026,10,9,3,50,tzinfo=timezone.utc)
        clock = StepClock(observed, 20)
        source = PageSource()
        async with service(system.db, source, now=clock.now) as calendar:
            wire(app, calendar, clock)
            success = await calendar.sync_full()
            assert success.run.start_date == DAY and success.run.end_date == DAY + timedelta(days=29)
            calls = len(source.calls)
            clock.at += timedelta(minutes=20)
            await app.reload()
            assert app.scope_matches and app.health_snapshot()["calendar_coverage_end"] == success.run.end_date.isoformat()
            with pytest.raises(EndReplay): await app.refresh_loop()
            assert len(source.calls) == calls
            await system.db.companies.upsert(Company("MSFT","789019","Added"))
            await app.reload()
            assert not app.scope_matches
    asyncio.run(scenario())


def test_daily_cadence_is_completion_based_and_near_term_is_disabled(system, tmp_path, no_backoff):
    async def scenario():
        app, _ = await setup(system, tmp_path, provider="yahoo")
        clock = StepClock(NOW, 86500)
        source = PageSource()
        async with service(system.db, source, now=clock.now) as calendar:
            wire(app, calendar, clock)
            recorded = []
            original = calendar.sync_full
            async def full():
                recorded.append(clock.monotonic())
                result = await original()
                clock.elapsed += 10  # Refresh itself takes time.
                return result
            calendar.sync_full = full
            async def near():
                pytest.fail("near-term refresh is disabled")
            calendar.sync_near_term = near
            with pytest.raises(EndReplay): await app.refresh_loop()
            assert recorded == [0,86410]
            assert app.reload_requested.is_set()
    asyncio.run(scenario())


def test_yahoo_satisfaction_survives_refresh_restart_but_is_not_transferred_to_a_move(system, tmp_path, no_backoff):
    async def scenario():
        app, _ = await setup(system, tmp_path, provider="yahoo")
        source = PageSource({DAY:[row(DAY)]})
        async with service(system.db, source) as calendar:
            wire(app, calendar, TestClock())
            await calendar.sync_full()
            observations = await app.worker.discovery.discover_with_evidence(COMPANY)
            await app.reload()
            await app.satisfy(observations)
            expected = app.scheduler.events[0]
            identity = EventIdentity.from_event(expected)
            assert identity in app.scheduler.satisfied
            await calendar.sync_full()
            restarted, _ = await setup(system, tmp_path, provider="yahoo")
            wire(restarted, calendar, TestClock())
            await restarted.reload()
            assert identity in restarted.scheduler.satisfied
            new_day = DAY + timedelta(days=1)
            source.days = {new_day:[row(new_day)]}
            await calendar.sync_full()
            await restarted.reload()
            moved = next(e for e in restarted.scheduler.events if e.expected_date == new_day)
            assert EventIdentity.from_event(moved) not in restarted.scheduler.satisfied
            assert await restarted.satisfaction.get(identity) is not None
            assert await restarted.satisfaction.get(EventIdentity.from_event(moved)) is None
    asyncio.run(scenario())
