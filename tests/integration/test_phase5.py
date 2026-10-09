"""Snapshot -> real calendar repositories -> durable success, with mocked AWS."""

import asyncio
from collections import deque
from dataclasses import replace
from datetime import date, timedelta
import json
import logging
from pathlib import Path

from botocore.exceptions import ReadTimeoutError
import pytest

from test_dynamodb_repositories import db, NOW
from finbot_ingestion.calendar import CalendarConfig, CalendarSnapshot
from finbot_ingestion.calendar.contracts import CalendarSyncRun, CalendarSyncSuccess
from finbot_ingestion.calendar.provider import CalendarDataError, CalendarTransientError, CalendarProviderNotConfigured, IncompleteCalendarSnapshot
from finbot_ingestion.calendar.service import CalendarSyncService
from finbot_ingestion.domain import Company, ExpectedEarningsEvent
from finbot_ingestion.repositories.dynamodb.serialization import decode, encode
from finbot_ingestion.repositories.errors import RepositoryConflict, RepositoryDataError
from finbot_ingestion.repositories.pagination import Page

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


def test_normalized_fixture_full_sync_and_repeated_refresh(db):
    async def scenario():
        payload = json.loads((Path(__file__).parents[1] / "fixtures/calendar_snapshot.json").read_text())
        values = []
        for row in payload["events"]:
            await db.companies.upsert(Company(row["ticker"], row["cik"], row["ticker"]))
            values.append(event(company_cik=row["cik"], ticker=row["ticker"], expected_date=date.fromisoformat(row["expected_date"]),
                                time_of_day=row["time_of_day"], provider_event_id=row["event_id"], raw_provider_payload=row))
        provider = FakeProvider(values)
        worker = service(db, provider)
        first = await worker.sync_full()
        second = await worker.sync_full()
        assert first.event_count == second.event_count == 3
        assert second.run.observed_at > first.run.observed_at
        records = await db.calendar.get_events(NOW, NOW + timedelta(days=2))
        assert {value.time_of_day for value in records} == {"before_market", "after_market", "unknown"}
        assert all(value.synced_at == second.run.observed_at for value in records)
        assert (await worker.health()).stale is False
        assert (await db.calendar.get_sync_state("fixture")).full_success == second
        assert all(operation in {"GetItem", "PutItem", "Query"} for operation, _ in db.memory.calls)
        assert not db.memory.tables["filings"] and not db.memory.tables["artifacts"]
    asyncio.run(scenario())


def test_date_move_cancellation_scope_and_material_logs(db, caplog):
    async def scenario():
        old = event()
        disabled = event(company_cik="2", ticker="OFF")
        other_provider = event(company_cik="3", ticker="OTHER", provider="other")
        outside = event(expected_date=NOW.date() + timedelta(days=20), provider_event_id="later")
        await seed(db, [old, disabled, other_provider, outside])
        await db.companies.upsert(Company("OFF", "2", "Disabled", enabled=False))
        moved = event(expected_date=NOW.date() + timedelta(days=1), time_of_day="after_market")
        provider = FakeProvider([moved])
        result = await service(db, provider).sync_near_term()
        assert result.cancelled_count == 1
        records = await db.calendar.get_events(NOW, NOW + timedelta(days=20))
        assert old not in records and len(records) == 4
        assert disabled in records and outside in records and other_provider in records
        assert len(provider.calls[0][2]) == 1
        assert any(row.get("calendar_active") is False for row in db.memory.tables["calendar"].values())
    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert {r.change for r in caplog.records if hasattr(r, "change")} == {"date_changed", "removed"}


def test_time_change_and_unknown_time(db, caplog):
    async def scenario():
        await seed(db, [event()])
        await service(db, FakeProvider([event(time_of_day=None)])).sync_full()
        assert (await db.calendar.get_events(NOW, NOW))[0].time_of_day == "unknown"
    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert any(getattr(row, "change", None) == "time_changed" for row in caplog.records)


@pytest.mark.parametrize("change,error", [
    ({"complete": False}, IncompleteCalendarSnapshot),
    ({"company_ciks": ()}, IncompleteCalendarSnapshot),
    ({"end_date": NOW.date()}, IncompleteCalendarSnapshot),
    ({"provider": "other"}, IncompleteCalendarSnapshot),
])
def test_incomplete_snapshot_preserves_expectations_and_previous_success(db, change, error):
    async def scenario():
        await seed(db)
        provider = FakeProvider([event()])
        worker = service(db, provider)
        first = await worker.sync_full()
        before = await db.calendar.get_events(NOW, NOW)
        provider.changes = change
        with pytest.raises(error):
            await worker.sync_full()
        assert await db.calendar.get_events(NOW, NOW) == before
        state = await db.calendar.get_sync_state("fixture")
        assert state.full_success == first and state.last_error == error.__name__
    asyncio.run(scenario())


@pytest.mark.parametrize("values", [
    [event(time_of_day="BMO")], [event(company_cik="999", ticker="OTHER")],
    [event(expected_date=NOW.date() - timedelta(days=1))], [event(provider="other")],
    [event(), event(time_of_day="after_market")],
    [event(), event(expected_date=NOW.date() + timedelta(days=1))],
    [event(raw_provider_payload={"bad": float("nan")})],
])
def test_invalid_snapshot_never_partially_writes_expectations(db, values):
    async def scenario():
        await seed(db, [event()])
        before = await db.calendar.get_events(NOW, NOW)
        with pytest.raises(CalendarDataError):
            await service(db, FakeProvider(values)).sync_full()
        assert await db.calendar.get_events(NOW, NOW) == before
        assert (await db.calendar.get_sync_state("fixture")).full_success is None
    asyncio.run(scenario())


def test_duplicates_aliases_and_multiple_quarters(db):
    async def scenario():
        await seed(db)
        value = event(ticker="PROVIDER_ALIAS")
        later = event(expected_date=NOW.date() + timedelta(days=60), provider_event_id="apple-q4")
        result = await service(db, FakeProvider([value, value, later])).sync_full()
        assert result.event_count == 2
        assert all(row.ticker == "AAPL" for row in await db.calendar.get_events(NOW, NOW + timedelta(days=89)))
    asyncio.run(scenario())


def test_complete_empty_snapshot_cancels_and_stale_retry_cannot_resurrect(db):
    async def scenario():
        old = event()
        await seed(db, [old])
        success = await service(db, FakeProvider()).sync_full()
        assert success.event_count == 0 and success.cancelled_count == 1
        await db.calendar.upsert_events([old])
        assert await db.calendar.get_events(NOW, NOW) == []
        with pytest.raises(RepositoryConflict):
            await db.calendar.upsert_events([replace(old, synced_at=success.run.observed_at)])
        newer = replace(old, synced_at=success.run.observed_at + timedelta(seconds=1))
        await db.calendar.upsert_events([newer])
        assert await db.calendar.get_events(NOW, NOW) == [newer]
    asyncio.run(scenario())


def test_placeholder_is_not_a_successful_empty_sync(db):
    async def scenario():
        await seed(db, [event(provider="placeholder")])
        worker = CalendarSyncService.with_placeholder(db.companies, db.calendar, now=lambda: NOW)
        with pytest.raises(CalendarProviderNotConfigured):
            await worker.sync_full()
        assert len(await db.calendar.get_events(NOW, NOW)) == 1
        state = await db.calendar.get_sync_state("placeholder")
        assert state.full_success is None and state.last_error == "CalendarProviderNotConfigured"
        assert (await worker.health()).stale
    asyncio.run(scenario())


def test_transient_failure_recovery_logs_never_echo_provider_secrets(db, caplog):
    async def scenario():
        await seed(db)
        provider = FakeProvider([event()])
        provider.failures.append(CalendarTransientError("https://private/?api_key=secret"))
        result = await service(db, provider).sync_full()
        assert result.event_count == 1 and len(provider.calls) == 2
    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert "Calendar operation recovered" in caplog.text and "api_key" not in caplog.text


@pytest.mark.parametrize("stage", ["begin", "upsert", "cancel", "complete"])
def test_lost_acknowledgments_replay_same_durable_operation(db, monkeypatch, stage):
    async def scenario():
        await seed(db, [event()])
        lost = False
        def api(operation, params):
            nonlocal lost
            response = db.memory.api(operation, params)
            item = decode(params["Item"]) if operation == "PutItem" else {}
            match = {"begin": item.get("calendar_record_type") == "sync" and "full_success" not in item,
                     "upsert": item.get("provider") == "fixture" and "calendar_active" not in item,
                     "cancel": item.get("calendar_active") is False,
                     "complete": "full_success" in item}[stage]
            if match and not lost:
                lost = True
                raise ReadTimeoutError(endpoint_url="https://mock.invalid")
            return response
        monkeypatch.setattr(db.client, "_make_api_call", api)
        moved = event(expected_date=NOW.date() + timedelta(days=1))
        result = await service(db, FakeProvider([moved])).sync_full()
        assert lost and result.event_count == result.cancelled_count == 1
        assert (await db.calendar.get_sync_state("fixture")).full_success == result
        assert [row.expected_date for row in await db.calendar.get_events(NOW, NOW + timedelta(days=2))] == [moved.expected_date]
    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["begin_sync", "upsert_events", "cancel_event", "complete_sync"])
def test_restart_after_each_durable_boundary_repairs_partial_refresh(db, monkeypatch, method):
    async def scenario():
        await seed(db, [event()])
        original = getattr(db.calendar, method)
        async def interrupted(*args, **kwargs):
            await original(*args, **kwargs)
            raise asyncio.CancelledError
        monkeypatch.setattr(db.calendar, method, interrupted)
        provider = FakeProvider([event(expected_date=NOW.date() + timedelta(days=1))])
        with pytest.raises(asyncio.CancelledError):
            await service(db, provider).sync_full()
        state = await db.calendar.get_sync_state("fixture")
        assert state.last_error is None
        assert (state.full_success is not None) == (method == "complete_sync")
        monkeypatch.setattr(db.calendar, method, original)
        restarted = await service(db, provider).sync_full()
        rows = await db.calendar.get_events(NOW, NOW + timedelta(days=2))
        assert len(rows) == 1 and rows[0].expected_date == NOW.date() + timedelta(days=1)
        assert (await db.calendar.get_sync_state("fixture")).full_success == restarted
    asyncio.run(scenario())


def test_database_failure_does_not_commit_success_and_restart_reconciles(db, monkeypatch):
    async def scenario():
        await seed(db, [event()])
        original = db.calendar.cancel_event
        async def fail(*args, **kwargs):
            raise RepositoryDataError("injected")
        monkeypatch.setattr(db.calendar, "cancel_event", fail)
        provider = FakeProvider([event(expected_date=NOW.date() + timedelta(days=1))])
        with pytest.raises(RepositoryDataError):
            await service(db, provider).sync_full()
        assert (await db.calendar.get_sync_state("fixture")).full_success is None
        assert len(await db.calendar.get_events(NOW, NOW + timedelta(days=2))) == 2
        monkeypatch.setattr(db.calendar, "cancel_event", original)
        await service(db, provider).sync_full()
        assert len(await db.calendar.get_events(NOW, NOW + timedelta(days=2))) == 1
    asyncio.run(scenario())


def test_near_term_success_does_not_refresh_full_watermark(db):
    async def scenario():
        await seed(db)
        provider = FakeProvider()
        worker = service(db, provider)
        full = await worker.sync_full()
        worker.now = lambda: NOW + timedelta(days=3)
        await worker.sync_near_term()
        health = await worker.health()
        assert health.stale and health.full_sync_age_seconds == 3 * 86400
        assert health.state.full_success == full and health.state.near_term_success is not None
    asyncio.run(scenario())


def test_pagination_continues_through_empty_company_and_tombstone_pages(db, monkeypatch):
    async def scenario():
        await seed(db, [event()])
        calls = []
        async def pages(**params):
            calls.append(params["token"])
            return Page((), "next") if params["token"] is None else Page((APPLE,))
        monkeypatch.setattr(db.companies, "list_enabled", pages)
        await service(db, FakeProvider()).sync_full()
        await db.calendar.upsert_events([event(company_cik="999", ticker="ZZZ")])
        db.memory.query_cap = 1
        assert [value.ticker for value in await db.calendar.get_events(NOW, NOW)] == ["ZZZ"]
        assert calls == [None, "next"]
    asyncio.run(scenario())


def test_newer_observations_survive_old_cancellation_and_provider_replacement_fails(db):
    async def scenario():
        current = event(synced_at=NOW + timedelta(days=1))
        await seed(db, [current])
        assert not await db.calendar.cancel_event(current, at=NOW)
        assert await db.calendar.get_events(NOW, NOW) == [current]
        with pytest.raises(RepositoryConflict):
            await db.calendar.upsert_events([event(provider="other", synced_at=NOW + timedelta(days=2))])
    asyncio.run(scenario())


def test_old_sync_completion_cannot_overwrite_newer_run(db):
    async def scenario():
        requested = CalendarSyncRun(request_id="first", provider="fixture", kind="full", start_date=NOW.date(),
            end_date=NOW.date(), company_ciks=(APPLE.cik,), observed_at=NOW)
        first = await db.calendar.begin_sync(requested)
        assert await db.calendar.begin_sync(requested) == first
        second = await db.calendar.begin_sync(replace(requested, request_id="second", observed_at=NOW - timedelta(days=1)))
        assert second.observed_at > first.observed_at
        with pytest.raises(RepositoryConflict):
            await db.calendar.complete_sync(CalendarSyncSuccess(run=first, completed_at=NOW, event_count=0, cancelled_count=0))
        with pytest.raises(RepositoryConflict):
            await db.calendar.begin_sync(replace(second, company_ciks=("2",)))
    asyncio.run(scenario())


@pytest.mark.parametrize("bound", ["max_companies", "max_snapshot_events"])
def test_configured_bounds_prevent_unbounded_refresh(db, bound):
    async def scenario():
        await seed(db)
        await db.companies.upsert(Company("OTHER", "2", "Other"))
        provider = FakeProvider([event(), event(company_cik="2", ticker="OTHER", provider_event_id="other")])
        with pytest.raises(CalendarDataError):
            await service(db, provider, **{bound: 1}).sync_full()
        assert await db.calendar.get_events(NOW, NOW) == []
    asyncio.run(scenario())


def test_empty_universe_and_ambiguous_tickers_do_not_claim_success(db):
    async def scenario():
        provider = FakeProvider()
        worker = service(db, provider)
        with pytest.raises(CalendarDataError):
            await worker.sync_full()
        await seed(db)
        await db.companies.upsert(Company("AAPL", "2", "Ambiguous"))
        with pytest.raises(CalendarDataError):
            await worker.sync_full()
        assert not provider.calls and await db.calendar.get_sync_state("fixture") is None
    asyncio.run(scenario())


def test_provider_error_propagates_even_if_failure_checkpoint_is_unavailable(db, monkeypatch):
    async def scenario():
        await seed(db, [event()])
        async def broken(*args, **kwargs):
            raise RepositoryDataError("offline")
        monkeypatch.setattr(db.calendar, "fail_sync", broken)
        provider = FakeProvider()
        provider.failures.append(CalendarDataError("original"))
        with pytest.raises(CalendarDataError, match="original"):
            await service(db, provider).sync_full()
        assert len(await db.calendar.get_events(NOW, NOW)) == 1
    asyncio.run(scenario())


def test_concurrent_calls_share_lock(db):
    async def scenario():
        await seed(db)
        provider = FakeProvider([event()])
        original = provider.fetch_events
        active = peak = 0
        async def fetch(*args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            result = await original(*args)
            active -= 1
            return result
        provider.fetch_events = fetch
        worker = service(db, provider)
        results = await asyncio.gather(worker.sync_full(), worker.sync_near_term())
        assert peak == 1 and results[1].run.observed_at > results[0].run.observed_at
    asyncio.run(scenario())


def test_exhausted_provider_budget_preserves_valid_expectations(db):
    async def scenario():
        await seed(db, [event()])
        provider = FakeProvider()
        provider.failures.extend(CalendarTransientError("unavailable") for _ in range(3))
        with pytest.raises(CalendarTransientError):
            await service(db, provider).sync_full()
        assert len(provider.calls) == 3 and len(await db.calendar.get_events(NOW, NOW)) == 1
        assert (await db.calendar.get_sync_state("fixture")).full_success is None
    asyncio.run(scenario())


def test_move_beyond_confirmed_range_does_not_erase_outside_expectation(db):
    async def scenario():
        outside = event(expected_date=NOW.date() + timedelta(days=20))
        await seed(db, [outside])
        await service(db, FakeProvider([event()])).sync_near_term()
        assert outside in await db.calendar.get_events(NOW, NOW + timedelta(days=20))
    asyncio.run(scenario())


def test_cancellation_cas_rereads_and_preserves_newer_row(db, monkeypatch):
    async def scenario():
        old = event()
        await seed(db, [old])
        changed = False
        def api(operation, params):
            nonlocal changed
            if operation == "PutItem" and decode(params["Item"]).get("calendar_active") is False and not changed:
                changed = True
                row = db.memory.tables["calendar"][(old.expected_date.isoformat(), old.company_cik)]
                row["synced_at"] = "2026-10-10T20:00:00.000000Z"
            return db.memory.api(operation, params)
        monkeypatch.setattr(db.client, "_make_api_call", api)
        success = await service(db, FakeProvider()).sync_full()
        assert changed and success.cancelled_count == 0
        assert len(await db.calendar.get_events(NOW, NOW)) == 1
    asyncio.run(scenario())


def test_entire_batch_size_validation_precedes_first_expectation_write(db):
    async def scenario():
        await seed(db)
        second = event(expected_date=NOW.date() + timedelta(days=1), provider_event_id="second",
                       raw_provider_payload={"too_large": "x" * (400 * 1024)})
        with pytest.raises(RepositoryDataError):
            await service(db, FakeProvider([event(), second])).sync_full()
        assert await db.calendar.get_events(NOW, NOW + timedelta(days=1)) == []
    asyncio.run(scenario())
