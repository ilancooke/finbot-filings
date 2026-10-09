"""Scheduling/evidence policies, bounds and telemetry without provider/AWS calls."""

import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import threading

import pytest

from finbot_ingestion.config import ConfigurationError
from finbot_ingestion.domain import Company, ExpectedEarningsEvent, Filing
from finbot_ingestion.domain.sec_items import FilingObservation, SECItemMetadata
from finbot_ingestion.domain.satisfaction import EventIdentity, EventSatisfaction
from finbot_ingestion.ingestion.work_queue import WorkQueue
from finbot_ingestion.runtime.config import RuntimeConfig
from finbot_ingestion.scheduler.market_sessions import ExchangeMarketSessions
from finbot_ingestion.scheduler.window_policy import EarningsWindow, WindowPolicy
from finbot_ingestion.scheduler.earnings_satisfaction_policy import EarningsSatisfactionPolicy, POLICY_VERSION
from finbot_ingestion.scheduler.polling_scheduler import PollingScheduler
from finbot_ingestion.sec.submissions import parse_company_submissions_with_evidence
from finbot_ingestion.observability.metrics import Metrics
from finbot_ingestion.observability.health import HealthFile
from finbot_ingestion.observability.logging import JSONFormatter

NOW = datetime(2026, 10, 8, 20, tzinfo=timezone.utc)
COMPANY = Company("AAPL", "320193", "Apple")
ACCESSION = "0000320193-26-000001"


class FakeClock:
    def __init__(self):
        self.elapsed = 0.0
        self.wall = NOW

    def monotonic(self):
        return self.elapsed

    def now(self):
        return self.wall + timedelta(seconds=self.elapsed)

    def advance(self, seconds):
        self.elapsed += seconds


def event(**changes):
    return replace(ExpectedEarningsEvent(company_cik=COMPANY.cik, ticker="AAPL", provider="fixture",
        expected_date=NOW.date(), time_of_day="after_market", synced_at=NOW, provider_event_id="quarter"), **changes)


def observation(form="8-K", status="known", items=("2.02",), **changes):
    filing = replace(Filing(accession_number=ACCESSION, company_cik=COMPANY.cik, ticker="AAPL",
        form_type=form, filed_at=NOW, discovered_at=NOW, filing_index_url="https://www.sec.gov/example"), **changes)
    return FilingObservation(filing, SECItemMetadata(filing.accession_number, filing.company_cik, status, items))


@pytest.mark.parametrize("form,status,items,expected", [
    ("10-Q", "absent", (), True), ("10-K", "ambiguous", (), True),
    ("8-K", "known", ("2.02",), True), ("8-K", "known", ("2.02", "9.01"), True),
    ("8-K", "absent", (), False), ("8-K", "ambiguous", (), False),
    ("8-K", "known", ("2.01", "9.01"), False),
    ("8-K/A", "known", ("2.02",), False), ("10-Q/A", "absent", (), False), ("10-K/A", "absent", (), False),
])
def test_exact_original_forms_and_item_policy(form, status, items, expected):
    supplied = observation(form, status, items)
    w = EarningsWindow(NOW - timedelta(hours=1), NOW + timedelta(hours=1), NOW + timedelta(hours=2))
    decision = EarningsSatisfactionPolicy().evaluate(event(), w, supplied.filing, supplied.sec_items)
    assert decision.eligible is expected and decision.policy_version == POLICY_VERSION
    if expected:
        checkpoint = EventSatisfaction.from_match(event(), w, supplied, decision, at=NOW)
        assert checkpoint.match_reason == decision.reason
    else:
        with pytest.raises(ValueError):
            EventSatisfaction.from_match(event(), w, supplied, decision, at=NOW)


@pytest.mark.parametrize("offset,eligible", [(-1, False), (0, True), (3599, True), (3600, False)])
def test_half_open_acceptance_window(offset, eligible):
    w = EarningsWindow(NOW, NOW + timedelta(minutes=30), NOW + timedelta(hours=1))
    supplied = observation(filed_at=NOW + timedelta(seconds=offset))
    assert EarningsSatisfactionPolicy().evaluate(event(), w, supplied.filing, supplied.sec_items).eligible is eligible


def test_wrong_identity_and_deterministic_selection():
    policy = EarningsSatisfactionPolicy()
    w = EarningsWindow(NOW - timedelta(hours=1), NOW + timedelta(hours=1), NOW + timedelta(hours=2))
    wrong = observation(company_cik="2")
    assert policy.evaluate(event(), w, wrong.filing, wrong.sec_items).reason == "cik_mismatch"
    late = observation(accession_number="0000320193-26-000002")
    early = observation()
    assert policy.select(event(), w, [late, early])[0] == early
    assert EventIdentity.from_event(event()) == EventIdentity.from_event(event(expected_date=date(2026, 10, 9)))
    assert EventIdentity.from_event(event(provider_event_id=None)) != EventIdentity.from_event(
        event(provider_event_id=None, expected_date=date(2026, 10, 9)))
    with pytest.raises(ValueError):
        replace(EventSatisfaction.from_match(event(), w, early, policy.evaluate(event(), w, early.filing,
            early.sec_items), at=NOW), match_policy_version="unknown")


@pytest.mark.parametrize("raw,status,items", [("2.02", "known", ("2.02",)),
    ("9.01, 2.02,2.02", "known", ("2.02", "9.01")), (None, "absent", ()), ("", "absent", ()),
    ("2.020", "ambiguous", ()), ("Item 2.02", "ambiguous", ()), (["2.02"], "ambiguous", ()),
    ("2.02;9.01", "ambiguous", ()), ("2.02,", "ambiguous", ()), ("2.02,9.٠١", "ambiguous", ())])
def test_item_source_normalization_and_duplicates(raw, status, items):
    payload = json.loads((Path(__file__).parents[1] / "fixtures/submissions_mixed.json").read_text())
    payload["filings"]["recent"]["items"] = [raw] * 8
    result = parse_company_submissions_with_evidence(COMPANY, payload, discovered_at=NOW)
    assert len(result) == 6
    assert all(o.sec_items.status == status and o.sec_items.items == items for o in result)
    payload["filings"]["recent"]["items"] = ["2.02"]
    assert all(o.sec_items.status == "ambiguous" for o in parse_company_submissions_with_evidence(
        COMPANY, payload, discovered_at=NOW))


def test_conflicting_duplicate_item_metadata_is_not_a_match():
    payload = json.loads((Path(__file__).parents[1] / "fixtures/submissions_mixed.json").read_text())
    payload["filings"]["recent"]["items"] = ["2.02"] * 8
    payload["filings"]["recent"]["items"][6] = "9.01"
    result = parse_company_submissions_with_evidence(COMPANY, payload, discovered_at=NOW)
    assert next(o for o in result if o.filing.accession_number == ACCESSION).sec_items.status == "ambiguous"


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


def test_queue_backpressure_cancellation_keeps_inflight_identity():
    async def scenario():
        q = WorkQueue(1, FakeClock())
        assert q.offer("a", "a")
        put = asyncio.create_task(q.put("b", "b"))
        await asyncio.sleep(0)
        put.cancel()
        with pytest.raises(asyncio.CancelledError):
            await put
        assert not q.contains("b")
        item = await q.get()
        assert not q.offer("a", "duplicate")
        assert q.offer("b", "b")
        q.done(item)
        q.discard()
        await q.queue.join()
        assert not q.keys
    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [{"active_poll_seconds": float("nan")}, {"grace_seconds": 0},
    {"poll_workers": True}, {"market_timezone": "absent"}, {"non_session_end_hour": 1},
    {"active_poll_seconds": 4000}, {"health_path": "relative"}])
def test_runtime_config_validation(changes):
    with pytest.raises(ConfigurationError):
        RuntimeConfig(**changes)


def test_environment_types_telemetry_bounds_and_sanitization(tmp_path):
    config = RuntimeConfig.from_env({"RUNTIME_POLL_WORKERS": "3", "RUNTIME_TICK_SECONDS": "0.5"})
    assert config.poll_workers == 3 and config.tick_seconds == .5
    output = []
    metrics = Metrics(sink=output.append)
    threads = [threading.Thread(target=lambda: [metrics.count("Requests") for _ in range(100)]) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for value in range(300):
        metrics.observe("Latency", value)
    metrics.flush()
    record = json.loads(output[0])
    assert record["Requests"] == 400 and len(record["Latency"]) == 100 and record["MetricSamplesDropped"] == 200
    assert record["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["Service", "Environment"]]
    with pytest.raises(ValueError):
        metrics.observe("Bad", float("inf"))
    log = logging.LogRecord("finbot_ingestion.test", logging.WARNING, "test", 1, "Provider failed", (), None)
    log.error_message, log.raw_payload = "secret", {"token": "secret"}
    assert "secret" not in JSONFormatter().format(log)
    path = tmp_path / "health.json"
    health = HealthFile(path)
    health.write({"heartbeat_at": NOW.isoformat(), "live": True})
    assert HealthFile.check(path, now=NOW)
    assert not HealthFile.check(path, now=NOW + timedelta(seconds=100))
    health.remove()
    assert not path.exists()
