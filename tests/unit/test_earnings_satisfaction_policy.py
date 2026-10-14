"""Original-form evidence and New York calendar-day earnings matching."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from finbot_ingestion.domain.satisfaction import EventIdentity, EventSatisfaction
from finbot_ingestion.runtime.config import RuntimeConfig
from finbot_ingestion.scheduler.earnings_satisfaction_policy import (
    EarningsSatisfactionPolicy,
    POLICY_VERSION,
)
from finbot_ingestion.scheduler.market_sessions import ExchangeMarketSessions
from finbot_ingestion.scheduler.window_policy import WindowPolicy

from tests.support.scheduling import NOW, event, observation


@pytest.mark.parametrize("form,status,items,expected", [
    ("10-Q", "absent", (), True), ("10-K", "ambiguous", (), True),
    ("8-K", "known", ("2.02",), True), ("8-K", "known", ("2.02", "9.01"), True),
    ("8-K", "absent", (), False), ("8-K", "ambiguous", (), False),
    ("8-K", "known", ("2.01", "9.01"), False),
    ("8-K/A", "known", ("2.02",), False), ("10-Q/A", "absent", (), False), ("10-K/A", "absent", (), False),
])
def test_exact_original_forms_and_item_policy(form, status, items, expected):
    supplied = observation(form, status, items)
    w = EarningsSatisfactionPolicy().window(event())
    decision = EarningsSatisfactionPolicy().evaluate(event(), supplied.filing, supplied.sec_items)
    assert decision.eligible is expected and decision.policy_version == POLICY_VERSION
    if expected:
        checkpoint = EventSatisfaction.from_match(event(), w, supplied, decision, at=NOW)
        assert checkpoint.match_reason == decision.reason
    else:
        with pytest.raises(ValueError):
            EventSatisfaction.from_match(event(), w, supplied, decision, at=NOW)


@pytest.mark.parametrize("offset,eligible", [(-1, False), (0, True), (86399, True), (86400, False)])
def test_half_open_acceptance_window(offset, eligible):
    policy = EarningsSatisfactionPolicy()
    w = policy.window(event())
    supplied = observation(filed_at=w.start + timedelta(seconds=offset))
    assert policy.evaluate(event(), supplied.filing, supplied.sec_items).eligible is eligible


def test_wrong_identity_and_deterministic_selection():
    policy = EarningsSatisfactionPolicy()
    w = policy.window(event())
    wrong = observation(company_cik="2")
    assert policy.evaluate(event(), wrong.filing, wrong.sec_items).reason == "cik_mismatch"
    late = observation(accession_number="0000320193-26-000002")
    early = observation()
    assert policy.select(event(), [late, early])[0] == early
    assert EventIdentity.from_event(event()) == EventIdentity.from_event(event(expected_date=date(2026, 10, 9)))
    assert EventIdentity.from_event(event(provider_event_id=None)) != EventIdentity.from_event(
        event(provider_event_id=None, expected_date=date(2026, 10, 9)))
    with pytest.raises(ValueError):
        replace(EventSatisfaction.from_match(event(), w, early, policy.evaluate(event(), early.filing,
            early.sec_items), at=NOW), match_policy_version="unknown")


@pytest.mark.parametrize("cik,ticker,accepted", [
    ("0000019617", "JPM", "2026-10-13T10:30:38Z"),
    ("0000731766", "UNH", "2026-10-13T10:14:45Z"),
    ("0000072971", "WFC", "2026-10-13T10:28:19Z"),
])
@pytest.mark.parametrize("timing", ["before_market", "after_market", "unknown", None])
def test_early_earnings_match_independently_of_polling(cik, ticker, accepted, timing):
    expected = event(company_cik=cik, ticker=ticker, expected_date=date(2026, 10, 13), time_of_day=timing)
    supplied = observation(company_cik=cik, ticker=ticker, filed_at=datetime.fromisoformat(accepted))
    polling = WindowPolicy(ExchangeMarketSessions(start=date(2026, 1, 1), end=date(2026, 12, 31)), RuntimeConfig())
    assert not polling.window(expected).contains(supplied.filing.filed_at)
    decision = EarningsSatisfactionPolicy().evaluate(expected, supplied.filing, supplied.sec_items)
    assert decision.eligible and decision.policy_version == "earnings-satisfaction-v2"


@pytest.mark.parametrize("day,start_hour,end_hour,hours", [
    (date(2026, 3, 8), 5, 4, 23), (date(2026, 11, 1), 4, 5, 25),
    (date(2026, 10, 10), 4, 4, 24), (date(2026, 7, 3), 4, 4, 24),
])
def test_matching_calendar_day_dst_weekend_and_holiday(day, start_hour, end_hour, hours):
    policy = EarningsSatisfactionPolicy()
    expected = event(expected_date=day)
    window = policy.window(expected)
    assert window.start == datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=start_hour)
    assert window.end == datetime.combine(day + timedelta(days=1), datetime.min.time(), timezone.utc) + timedelta(hours=end_hour)
    assert window.grace_end == window.end and window.end - window.start == timedelta(hours=hours)
    for at, eligible in ((window.start - timedelta(microseconds=1), False), (window.start, True),
                         (window.end - timedelta(microseconds=1), True), (window.end, False)):
        supplied = observation(filed_at=at)
        assert policy.evaluate(expected, supplied.filing, supplied.sec_items).eligible is eligible


def test_legacy_bounds_retained_but_v2_requires_exact_calendar_day():
    expected, supplied, policy = event(), observation(), EarningsSatisfactionPolicy()
    value = EventSatisfaction.from_match(expected, policy.window(expected), supplied,
        policy.evaluate(expected, supplied.filing, supplied.sec_items), at=NOW)
    polling = WindowPolicy(ExchangeMarketSessions(start=date(2026, 1, 1), end=date(2026, 12, 31)), RuntimeConfig()).window(expected)
    legacy = replace(value, match_policy_version="earnings-satisfaction-v1", window_start=polling.start,
                     window_end=polling.end, grace_end=polling.grace_end)
    assert legacy.match_policy_version == "earnings-satisfaction-v1"
    with pytest.raises(ValueError, match="exact New York"):
        replace(legacy, match_policy_version="earnings-satisfaction-v2")
    with pytest.raises(ValueError, match="exact New York"):
        replace(value, grace_end=value.grace_end + timedelta(seconds=1))


@pytest.mark.parametrize("changes", [{"company_cik": "2"}, {"accession_number": "0000320193-26-000002"}])
def test_8k_evidence_must_identify_the_filing(changes):
    supplied = observation()
    evidence = replace(supplied.sec_items, **changes)
    assert not EarningsSatisfactionPolicy().evaluate(event(), supplied.filing, evidence).eligible


def six_k_progress(**changes):
    from finbot_ingestion.domain import FilingCheckpoint
    from finbot_ingestion.selection.policy import POLICY_VERSION as selection_version
    return replace(FilingCheckpoint(accession_number=observation().filing.accession_number,
        selection_policy=selection_version, selection_decision='SELECT', selection_reason='earnings_6_k',
        selection_checked_at=NOW, enumeration_completed_at=NOW, resolved_primary_document_name='Primary.htm',
        enumerated_artifact_count=1), **changes)


@pytest.mark.parametrize('changes', [
    {'selection_decision': 'UNRESOLVED'}, {'selection_decision': 'EXCLUDE'},
    {'selection_reason': 'primary_report'}, {'selection_policy': 'earnings-acquisition-v2'},
    {'accession_number': '0000320193-26-000099'},
    {'enumeration_completed_at': None, 'resolved_primary_document_name': None, 'enumerated_artifact_count': None},
])
def test_6k_requires_its_completed_affirmative_selection(changes):
    supplied = observation('6-K', 'known', ('2.02',))
    policy = EarningsSatisfactionPolicy()
    assert not policy.evaluate(event(), supplied.filing, supplied.sec_items).eligible
    assert not policy.evaluate(event(), supplied.filing, supplied.sec_items, selection=six_k_progress(**changes)).eligible


@pytest.mark.parametrize('form,at,cik,eligible', [
    ('6-K', NOW, '320193', True), ('6-K/A', NOW, '320193', False),
    ('6-K', NOW, '2', False),
    ('6-K', datetime(2026, 10, 8, 3, 59, 59, tzinfo=timezone.utc), '320193', False),
    ('6-K', datetime(2026, 10, 9, 4, tzinfo=timezone.utc), '320193', False),
])
def test_6k_matching_retains_identity_date_and_amendment_safeguards(form, at, cik, eligible):
    supplied = observation(form, 'absent', (), filed_at=at, company_cik=cik)
    policy = EarningsSatisfactionPolicy()
    decision = policy.evaluate(event(), supplied.filing, supplied.sec_items, selection=six_k_progress())
    assert decision.eligible is eligible
    if eligible:
        value = EventSatisfaction.from_match(event(), policy.window(event()), supplied, decision, at=NOW)
        assert value.match_policy_version == 'earnings-satisfaction-v3'
        assert value.matched_sec_items == () and value.sec_item_evidence_source is None
        with pytest.raises(ValueError, match='calendar-day bounds'):
            replace(value, grace_end=value.grace_end + timedelta(seconds=1))
