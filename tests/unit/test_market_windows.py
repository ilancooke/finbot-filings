"""Market sessions, polling windows and reload coverage."""

from datetime import date, timedelta

import pytest

from finbot_ingestion.runtime.config import RuntimeConfig
from finbot_ingestion.scheduler.market_sessions import ExchangeMarketSessions
from finbot_ingestion.scheduler.window_policy import WindowPolicy

from tests.support.scheduling import NOW, event


@pytest.mark.parametrize("day,open_hour,close_hour", [(date(2026, 3, 6), 14, 21),
    (date(2026, 3, 9), 13, 20), (date(2026, 11, 27), 14, 18)])
def test_real_offline_exchange_dst_and_early_close(day, open_hour, close_hour):
    sessions = ExchangeMarketSessions(start=date(2026, 1, 1), end=date(2026, 12, 31))
    opened, closed = sessions.session(day)
    assert opened.hour == open_hour and closed.hour == close_hour
    w = WindowPolicy(sessions, RuntimeConfig()).window(event(expected_date=day))
    assert w.start == closed - timedelta(hours=2)


@pytest.mark.parametrize("day", [date(2026, 7, 3), date(2026, 10, 10)])
def test_non_session_is_not_shifted(day):
    sessions = ExchangeMarketSessions(start=date(2026, 1, 1), end=date(2026, 12, 31))
    assert sessions.session(day) is None
    w = WindowPolicy(sessions, RuntimeConfig()).window(event(expected_date=day))
    assert w.start.date() == day and w.start.hour == 11 and w.start.minute == 30
    assert w.grace_end.date() > day
    with pytest.raises(ValueError):
        sessions.session(date(2030, 1, 1))


@pytest.mark.parametrize("report_time", ["before_market", "unknown", None])
def test_open_unknown_windows_and_extended_reload_coverage(report_time):
    sessions = ExchangeMarketSessions(start=date(2026, 1, 1), end=date(2026, 12, 31))
    config = RuntimeConfig(before_open_seconds=3 * 86400, grace_seconds=2 * 86400)
    policy = WindowPolicy(sessions, config)
    w = policy.window(event(time_of_day=report_time))
    opened, closed = sessions.session(NOW.date())
    assert w.start == opened - timedelta(days=3)
    assert w.end == (opened + timedelta(hours=2) if report_time == "before_market" else closed + timedelta(hours=3))
    assert w.grace_end == w.end + timedelta(days=2)
    assert policy.forward_days >= 4 and policy.lookback_days >= 5
    assert w.contains(w.grace_end - timedelta(microseconds=1)) and not w.contains(w.grace_end)
