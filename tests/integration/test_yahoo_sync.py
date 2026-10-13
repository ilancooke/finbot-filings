"""Raw Yahoo pages -> real durable calendar repository, with offline SDK boundaries."""

from dataclasses import replace
from datetime import timedelta
import asyncio

from botocore.exceptions import ReadTimeoutError
import pytest

from finbot_ingestion.calendar.provider import IncompleteCalendarSnapshot
from finbot_ingestion.domain import Company, ExpectedEarningsEvent
from finbot_ingestion.repositories.dynamodb.serialization import record, restore

from tests.support.dynamodb import NOW
from tests.support.yahoo_fakes import PageSource, row
from tests.support.yahoo_sync import COMPANY, DAY, event, service, seed


def test_absence_never_cancels_but_successful_empty_collection_advances_freshness(db):
    async def scenario():
        old = event()
        await seed(db, [old])
        async with service(db, PageSource()) as calendar:
            success = await calendar.sync_once(DAY, DAY)
            assert success.event_count == success.cancelled_count == 0
            assert await db.calendar.get_event(DAY, COMPANY.cik) == old
            assert not (await calendar.health()).stale
    asyncio.run(scenario())


def test_unique_move_is_confirmed_before_cancellation_and_other_quarters_survive(db):
    async def scenario():
        old = event(DAY - timedelta(days=1))
        later = event(DAY + timedelta(days=3), replacement_hint="quarterly-announcement-v1/2026/Q4")
        await seed(db, [old, later])
        async with service(db, PageSource({DAY:[row(DAY)]})) as calendar:
            success = await calendar.sync_once(DAY, DAY + timedelta(days=3))
            assert success.cancelled_count == 1
            assert await db.calendar.get_event(old.expected_date, COMPANY.cik) is None
            assert await db.calendar.get_event(later.expected_date, COMPANY.cik) == later
            moved = await db.calendar.get_event(DAY, COMPANY.cik)
            assert moved.replacement_hint == old.replacement_hint and moved.synced_at == success.run.observed_at
            operations = db.memory.calls
            cancellation = next(i for i, (op, p) in enumerate(operations) if op == "PutItem" and p["Item"].get("calendar_active") == {"BOOL":False})
            assert any(op == "GetItem" and p["Key"].get("expected_date") == {"S":DAY.isoformat()}
                       for op,p in operations[:cancellation])
    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["missing_hint", "multiple_old", "multiple_new", "both_dates", "different_quarter", "out_of_range"])
def test_ambiguous_or_unconfirmed_moves_preserve_old_expectations(db, case):
    async def scenario():
        old = event()
        if case == "missing_hint": old = replace(old, replacement_hint=None)
        values = [old]
        new_day = DAY + timedelta(days=1)
        days = {new_day:[row(new_day)]}
        if case == "multiple_old": values.append(event(DAY - timedelta(days=1)))
        if case == "multiple_new": days[DAY + timedelta(days=2)] = [row(DAY + timedelta(days=2))]
        if case == "both_dates": days[DAY] = [row(DAY)]
        if case == "different_quarter": days[new_day] = [row(new_day, title="Q4 2026 Earnings Announcement")]
        if case == "out_of_range": days = {DAY + timedelta(days=30):[row(DAY + timedelta(days=30))]}
        await seed(db, values)
        async with service(db, PageSource(days)) as calendar:
            success = await calendar.sync_once(DAY, DAY + timedelta(days=2))
            assert success.cancelled_count == 0
            assert await db.calendar.get_event(DAY, COMPANY.cik) is not None
    asyncio.run(scenario())


def test_partial_fetch_never_writes_expectations_or_success(db):
    async def scenario():
        old = event()
        await seed(db, [old])
        def incomplete(payload, *args):
            payload["finance"]["result"][0]["total"] += 1
        async with service(db, PageSource({DAY:[row(DAY)]}, incomplete)) as calendar:
            with pytest.raises(IncompleteCalendarSnapshot): await calendar.sync_once(DAY, DAY)
            state = await db.calendar.get_sync_state("yahoo")
            assert state.full_success is None and state.failure_run is not None
            assert await db.calendar.get_event(DAY, COMPANY.cik) == old
    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["upsert_events", "cancel_event", "complete_sync"])
def test_crash_after_each_apply_boundary_is_repaired_from_a_new_snapshot(db, monkeypatch, method):
    async def scenario():
        old = event()
        moved_day = DAY + timedelta(days=1)
        await seed(db, [old])
        original = getattr(db.calendar, method)
        first = True
        async def interrupted(*args, **kwargs):
            nonlocal first
            result = await original(*args, **kwargs)
            if first:
                first = False
                raise RuntimeError("simulated process interruption after durable write")
            return result
        monkeypatch.setattr(db.calendar, method, interrupted)
        async with service(db, PageSource({moved_day:[row(moved_day)]})) as calendar:
            with pytest.raises(RuntimeError): await calendar.sync_once(DAY, moved_day)
        monkeypatch.setattr(db.calendar, method, original)
        async with service(db, PageSource({moved_day:[row(moved_day)]})) as restarted:
            await restarted.sync_once(DAY, moved_day)
            assert await db.calendar.get_event(DAY, COMPANY.cik) is None
            assert await db.calendar.get_event(moved_day, COMPANY.cik) is not None
            assert (await db.calendar.get_sync_state("yahoo")).full_success is not None
    asyncio.run(scenario())


def test_lost_cancel_acknowledgment_is_idempotent(db, monkeypatch):
    async def scenario():
        old = event()
        new_day = DAY + timedelta(days=1)
        await seed(db, [old])
        original = db.calendar.cancel_event
        first = True
        async def lost(*args, **kwargs):
            nonlocal first
            result = await original(*args, **kwargs)
            if first:
                first = False
                raise ReadTimeoutError(endpoint_url="https://fixture.invalid")
            return result
        monkeypatch.setattr(db.calendar, "cancel_event", lost)
        async with service(db, PageSource({new_day:[row(new_day)]})) as calendar:
            success = await calendar.sync_once(DAY, new_day)
            assert success.cancelled_count == 1
    asyncio.run(scenario())


def test_changed_old_observation_is_not_cancelled_even_when_older_than_run(db, monkeypatch):
    async def scenario():
        old = event()
        new_day = DAY + timedelta(days=1)
        await seed(db, [old])
        original = db.calendar.cancel_event
        changed = replace(old, synced_at=NOW-timedelta(seconds=1), replacement_hint="quarterly-announcement-v1/2026/Q4")
        async def race(value, **kwargs):
            await db.calendar.upsert_events([changed])
            return await original(value, **kwargs)
        monkeypatch.setattr(db.calendar, "cancel_event", race)
        async with service(db, PageSource({new_day:[row(new_day)]})) as calendar:
            result = await calendar.sync_once(DAY, new_day)
            assert result.cancelled_count == 0
            assert await db.calendar.get_event(DAY, COMPANY.cik) == changed
    asyncio.run(scenario())


def test_ignored_stale_replacement_write_cannot_cancel_old_event(db):
    async def scenario():
        old = event()
        new_day = DAY + timedelta(days=1)
        newer = replace(event(new_day), synced_at=NOW+timedelta(seconds=1), replacement_hint="quarterly-announcement-v1/2026/Q4")
        await seed(db, [old, newer])
        async with service(db, PageSource({new_day:[row(new_day)]})) as calendar:
            with pytest.raises(IncompleteCalendarSnapshot): await calendar.sync_once(DAY, new_day)
            assert await db.calendar.get_event(DAY, COMPANY.cik) == old
            assert await db.calendar.get_event(new_day, COMPANY.cik) == newer
    asyncio.run(scenario())


def test_provider_and_disabled_company_records_are_not_reconciled(db):
    async def scenario():
        other = replace(event(), provider="other", company_cik="2", ticker="OTHER")
        disabled = replace(event(), company_cik="3", ticker="OFF")
        await seed(db, [other, disabled])
        await db.companies.upsert(Company("OFF", "3", "Disabled", enabled=False))
        async with service(db, PageSource()) as calendar:
            await calendar.sync_once(DAY, DAY)
            assert await db.calendar.get_event(DAY, "2") == other
            assert await db.calendar.get_event(DAY, "3") == disabled
    asyncio.run(scenario())


def test_additive_evidence_roundtrip_and_old_record_default():
    value = event()
    item = record(value)
    assert restore(ExpectedEarningsEvent, item) == value
    item.pop("replacement_hint")
    assert restore(ExpectedEarningsEvent, item).replacement_hint is None
    with pytest.raises(ValueError): replace(value, replacement_hint="unversioned")

# Bypass provider pacing only in tests that use the synthetic Yahoo boundary.
pytestmark = pytest.mark.usefixtures("no_backoff")
