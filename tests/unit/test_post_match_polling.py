"""Fixed follow-up deadlines, independently of the original polling window."""

from datetime import timedelta

import pytest

from finbot_ingestion.domain.satisfaction import EventIdentity
from finbot_ingestion.ingestion.work_queue import WorkQueue
from finbot_ingestion.runtime.config import RuntimeConfig
from finbot_ingestion.scheduler.market_sessions import ExchangeMarketSessions
from finbot_ingestion.scheduler.polling_scheduler import PollingScheduler
from finbot_ingestion.scheduler.window_policy import WindowPolicy

from tests.support.scheduling import COMPANY, FakeClock, event


def scheduler(clock=None):
    clock = clock or FakeClock()
    config = RuntimeConfig()
    sessions = ExchangeMarketSessions(start=clock.now().date() - timedelta(days=365),
                                     end=clock.now().date() + timedelta(days=365))
    queue = WorkQueue(10, clock)
    result = PollingScheduler(config, clock, WindowPolicy(sessions, config), queue)
    result.reload([COMPANY], [event()], set())
    return result, clock, queue


def test_recent_match_keeps_active_interval_until_fixed_ten_minute_boundary():
    polls, clock, _ = scheduler()
    identity = EventIdentity.from_event(event())
    polls.mark_satisfied(identity, at=clock.now())
    polls.completed(COMPANY.cik)
    assert polls.states[COMPANY.cik].due == 10
    clock.advance(599.999)
    assert polls.active(COMPANY.cik) and polls.active_count == 1
    clock.advance(.001)
    assert polls.windows.window(event()).contains(clock.now())
    assert not polls.active(COMPANY.cik) and polls.active_count == 0
    polls.completed(COMPANY.cik)
    assert polls.states[COMPANY.cik].due == clock.monotonic() + 3600


def test_later_observations_and_calendar_reload_do_not_extend_deadline():
    polls, clock, _ = scheduler()
    identity, original = EventIdentity.from_event(event()), clock.now()
    polls.mark_satisfied(identity, at=original)
    clock.advance(300)
    polls.mark_satisfied(identity, at=clock.now())
    assert polls.satisfied_at[identity] == original
    polls.reload([COMPANY], [event()], {identity}, satisfied_at={identity: original})
    assert polls.active(COMPANY.cik)
    clock.advance(300)
    assert not polls.active(COMPANY.cik)


@pytest.mark.parametrize("elapsed,active", [(300, True), (600, False), (3600, False)])
def test_restart_resumes_only_remaining_follow_up(elapsed, active):
    clock = FakeClock()
    original = clock.now()
    clock.advance(elapsed)
    polls, _, queue = scheduler(clock)
    identity = EventIdentity.from_event(event())
    polls.reload([COMPANY], [event()], {identity}, satisfied_at={identity: original})
    assert polls.active(COMPANY.cik) is active
    polls.tick()
    assert queue.contains(COMPANY.cik) is active


@pytest.mark.parametrize("position", ["before", "end"])
def test_follow_up_is_not_limited_to_original_polling_window(position):
    polls, clock, _ = scheduler()
    window = polls.windows.window(event())
    clock.wall = window.start - timedelta(hours=1) if position == "before" else window.grace_end - timedelta(seconds=30)
    polls.mark_satisfied(EventIdentity.from_event(event()), at=clock.now())
    clock.advance(60)
    assert not window.contains(clock.now())
    assert polls.active(COMPANY.cik)
    clock.advance(540)
    assert not polls.active(COMPANY.cik)


def test_follow_up_preserves_retry_backoff_and_disabling_company():
    polls, clock, queue = scheduler()
    identity = EventIdentity.from_event(event())
    polls.mark_satisfied(identity, at=clock.now())
    polls.completed(COMPANY.cik, retry_delay=30)
    assert polls.states[COMPANY.cik].due == 30
    polls.reload([], [], set(), satisfied_at={identity: clock.now()})
    polls.tick()
    assert not polls.states and not polls.satisfied_at and not queue.keys
