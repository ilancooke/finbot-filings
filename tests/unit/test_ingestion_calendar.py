"""Calendar contracts/configuration are pure, provider-independent and offline."""

import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from finbot_ingestion.calendar import CalendarConfig, CalendarSnapshot, CalendarSyncRun, CalendarSyncState, CalendarSyncSuccess
from finbot_ingestion.calendar.provider import CalendarProviderNotConfigured
from finbot_ingestion.calendar.providers import PlaceholderCalendarProvider
from finbot_ingestion.config import ConfigurationError, IngestionConfig
from finbot_ingestion.repositories.dynamodb.calendar_state import read_state, state_record
from finbot_ingestion.repositories.dynamodb.serialization import decode, encode
from finbot_ingestion.repositories.errors import RepositoryDataError

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


def run(**changes):
    return replace(CalendarSyncRun(request_id="request", provider="fixture", kind="full",
        start_date=NOW.date(), end_date=NOW.date(), company_ciks=("320193",), observed_at=NOW), **changes)


def test_config_is_isolated_and_does_not_resolve_credentials(monkeypatch):
    import boto3
    monkeypatch.setattr(boto3, "Session", lambda *a, **k: pytest.fail("client construction"))
    assert CalendarConfig.from_env({}).provider == "placeholder"
    config = CalendarConfig.from_env({"CALENDAR_LOOKAHEAD_DAYS": "14", "CALENDAR_NEAR_TERM_REFRESH_SECONDS": "14400"})
    assert config.lookahead_days == 14 and config.near_term_refresh_seconds == 14400
    assert IngestionConfig.from_env({"SEC_USER_AGENT": "Finbot owner@example.com"})


@pytest.mark.parametrize("changes", [
    {"provider": ""}, {"provider": "INVALID"}, {"provider": "secret/key"},
    {"lookahead_days": 0}, {"lookahead_days": True}, {"near_term_days": 91},
    {"company_page_size": 1001}, {"provider_attempts": 0}, {"checkpoint_attempts": 0},
    {"max_snapshot_events": 0}, {"max_companies": 0}, {"full_refresh_seconds": float("nan")},
    {"near_term_refresh_seconds": -1}, {"stale_after_seconds": 0},
    {"backoff_base_seconds": float("inf")}, {"backoff_cap_seconds": .5},
])
def test_invalid_calendar_configuration(changes):
    with pytest.raises(ConfigurationError):
        CalendarConfig(**changes)


@pytest.mark.parametrize("value", ["no", "1.5", "nan"])
def test_invalid_calendar_environment(value):
    with pytest.raises(ConfigurationError):
        CalendarConfig.from_env({"CALENDAR_PROVIDER_ATTEMPTS": value})


def test_placeholder_raises_instead_of_returning_empty_calendar():
    with pytest.raises(CalendarProviderNotConfigured):
        asyncio.run(PlaceholderCalendarProvider().fetch_events(NOW.date(), NOW.date(), ()))


@pytest.mark.parametrize("changes", [
    {"provider": "bad/provider"}, {"kind": "daily"}, {"request_id": ""},
    {"start_date": NOW}, {"end_date": NOW.date() - timedelta(days=1)},
    {"company_ciks": ("320193", "0000320193")}, {"observed_at": NOW.replace(tzinfo=None)},
])
def test_invalid_run(changes):
    with pytest.raises(ValueError):
        run(**changes)


def test_scope_normalization_and_utc_time():
    value = run(company_ciks=("10", "2"), observed_at=NOW.astimezone(timezone(timedelta(hours=-7))))
    assert value.company_ciks == ("0000000002", "0000000010") and value.observed_at == NOW
    snapshot = CalendarSnapshot(provider="fixture", start_date=NOW.date(), end_date=NOW.date(),
                                company_ciks=("320193",), events=(), complete=True)
    assert snapshot.company_ciks == ("0000320193",)
    with pytest.raises(ValueError):
        replace(snapshot, complete=1)


def test_sync_state_round_trip_and_separate_successes():
    full = CalendarSyncSuccess(run=run(), completed_at=NOW, event_count=1, cancelled_count=0)
    near = CalendarSyncSuccess(run=run(request_id="near", kind="near_term", observed_at=NOW + timedelta(seconds=1)),
                               completed_at=NOW + timedelta(seconds=1), event_count=0, cancelled_count=1)
    state = CalendarSyncState(provider="fixture", revision=3, latest_run=near.run,
                              full_success=full, near_term_success=near)
    assert read_state(decode(encode(state_record(state)))) == state
    failed = replace(state, failure_run=near.run, last_error="CalendarDataError", last_error_at=near.completed_at)
    assert read_state(decode(encode(state_record(failed)))) == failed


@pytest.mark.parametrize("changes", [
    {"revision": True}, {"repository_schema_version": 2}, {"expected_date": "2026-10-08"},
    {"calendar_record_type": "event"}, {"last_error": "unpaired"},
    {"latest_run": {"bad": "run"}},
])
def test_corrupt_sync_state_rejected(changes):
    item = state_record(CalendarSyncState(provider="fixture", revision=0, latest_run=run()))
    with pytest.raises(RepositoryDataError):
        read_state({**item, **changes})


def test_invalid_completion_and_failure_facts():
    with pytest.raises(ValueError):
        CalendarSyncSuccess(run=run(), completed_at=NOW - timedelta(seconds=1), event_count=0, cancelled_count=0)
    with pytest.raises(ValueError):
        CalendarSyncSuccess(run=run(), completed_at=NOW, event_count=True, cancelled_count=0)
    with pytest.raises(ValueError):
        CalendarSyncState(provider="fixture", revision=0, latest_run=run(), last_error="unpaired")
