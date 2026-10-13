"""Completion-based scheduling and queue admission."""

from datetime import date
import asyncio

from finbot_ingestion.domain import Company
from finbot_ingestion.domain.satisfaction import EventIdentity
from finbot_ingestion.ingestion.work_queue import WorkQueue
from finbot_ingestion.runtime.config import RuntimeConfig
from finbot_ingestion.scheduler.market_sessions import ExchangeMarketSessions
from finbot_ingestion.scheduler.polling_scheduler import PollingScheduler
from finbot_ingestion.scheduler.window_policy import WindowPolicy

from tests.support.scheduling import COMPANY, FakeClock, event


def test_scheduler_transition_completion_satisfaction_and_queue_full():
    clock = FakeClock()
    sessions = ExchangeMarketSessions(start=date(2026, 1, 1), end=date(2026, 12, 31))
    queue = WorkQueue(1, clock)
    scheduler = PollingScheduler(RuntimeConfig(), clock, WindowPolicy(sessions, RuntimeConfig()), queue)
    other = Company("OTHER", "1", "Other")
    scheduler.reload([COMPANY, other], [event()], set())
    scheduler.tick()
    assert queue.contains(COMPANY.cik)
    scheduler.tick()
    assert len(queue.keys) == 1
    async def scenario():
        item = await queue.get()
        scheduler.satisfied.add(EventIdentity.from_event(event()))
        scheduler.completed(item.key)
        queue.done(item)
        assert scheduler.states[item.key].due == clock.monotonic() + 3600
        clock.advance(10000)
        scheduler.tick()
        assert len(queue.keys) == 1  # no catch-up burst
        scheduler.reload([other], [], set())
        assert COMPANY.cik not in scheduler.states
    asyncio.run(scenario())
