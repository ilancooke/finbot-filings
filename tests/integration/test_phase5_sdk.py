"""Real botocore models/Stubber validate the calendar-table wire contract."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from test_dynamodb_sdk import sdk
from finbot_ingestion.calendar.contracts import CalendarSyncRun, CalendarSyncState, CalendarSyncSuccess
from finbot_ingestion.domain import ExpectedEarningsEvent
from finbot_ingestion.repositories.dynamodb import DynamoDBCalendarRepository
from finbot_ingestion.repositories.dynamodb.calendar_state import SYNC_PARTITION, state_record
from finbot_ingestion.repositories.dynamodb.serialization import encode, record, timestamp

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


def event():
    return ExpectedEarningsEvent(company_cik="320193", ticker="AAPL", expected_date=NOW.date(),
                                 synced_at=NOW, provider="fixture", time_of_day="unknown")


def test_sdk_calendar_upsert_is_provider_and_timestamp_guarded(sdk):
    _, stub, execution = sdk
    value = event()
    stub.add_response("put_item", {}, {"TableName": "calendar", "Item": encode(record(value)),
        "ConditionExpression": "attribute_not_exists(#date) OR #synced < :synced AND #provider = :provider",
        "ExpressionAttributeNames": {"#date": "expected_date", "#synced": "synced_at", "#provider": "provider"},
        "ExpressionAttributeValues": encode({":synced": timestamp(NOW), ":provider": "fixture"})})
    asyncio.run(DynamoDBCalendarRepository(execution).upsert_events([value]))


def test_sdk_replacement_base_read_and_guarded_cancellation(sdk):
    _, stub, execution = sdk
    value = replace(event(), provider="yahoo", replacement_hint="quarterly-announcement-v1/2026/Q3")
    at = NOW.replace(hour=1)
    get = {"TableName":"calendar", "Key":encode({"expected_date":NOW.date().isoformat(), "cik":value.company_cik}),
           "ConsistentRead":True}
    stub.add_response("get_item", {"Item":encode(record(value))}, get)
    stub.add_response("get_item", {"Item":encode(record(value))}, get)
    tombstone = {**record(replace(value, synced_at=at)), "calendar_active":False}
    stub.add_response("put_item", {}, {"TableName":"calendar", "Item":encode(tombstone),
        "ConditionExpression":"attribute_exists(#date) AND #synced = :old",
        "ExpressionAttributeNames":{"#date":"expected_date", "#synced":"synced_at"},
        "ExpressionAttributeValues":encode({":old":timestamp(NOW)})})
    async def scenario():
        repository = DynamoDBCalendarRepository(execution)
        assert await repository.get_event(value.expected_date, value.company_cik) == value
        assert await repository.cancel_event(value, at=at, require_unchanged=True)
    asyncio.run(scenario())


def test_sdk_replacement_guard_rejects_changed_source_without_a_write(sdk):
    _, stub, execution = sdk
    value = replace(event(), provider="yahoo", replacement_hint="quarterly-announcement-v1/2026/Q3")
    changed = replace(value, synced_at=NOW.replace(minute=1), replacement_hint="quarterly-announcement-v1/2026/Q4")
    stub.add_response("get_item", {"Item":encode(record(changed))}, {"TableName":"calendar",
        "Key":encode({"expected_date":NOW.date().isoformat(), "cik":value.company_cik}), "ConsistentRead":True})
    async def scenario():
        assert not await DynamoDBCalendarRepository(execution).cancel_event(value, at=NOW.replace(hour=1), require_unchanged=True)
    asyncio.run(scenario())


def test_sdk_tombstone_conditional_write_and_lost_acknowledgment_replay(sdk):
    _, stub, execution = sdk
    value = event()
    at = NOW.replace(hour=1)
    key = encode({"expected_date": "2026-10-08", "cik": value.company_cik})
    get = {"TableName": "calendar", "Key": key, "ConsistentRead": True}
    tombstone = {**record(replace(value, synced_at=at)), "calendar_active": False}
    stub.add_response("get_item", {"Item": encode(record(value))}, get)
    stub.add_response("put_item", {}, {"TableName": "calendar", "Item": encode(tombstone),
        "ConditionExpression": "attribute_exists(#date) AND #synced = :old",
        "ExpressionAttributeNames": {"#date": "expected_date", "#synced": "synced_at"},
        "ExpressionAttributeValues": encode({":old": timestamp(NOW)})})
    stub.add_response("get_item", {"Item": encode(tombstone)}, get)
    async def scenario():
        repository = DynamoDBCalendarRepository(execution)
        assert await repository.cancel_event(value, at=at)
        assert await repository.cancel_event(value, at=at)
    asyncio.run(scenario())


def test_sdk_begin_and_complete_sync_use_reserved_key_and_revision_guard(sdk):
    _, stub, execution = sdk
    run = CalendarSyncRun(request_id="request", provider="fixture", kind="full", start_date=NOW.date(),
                         end_date=NOW.date(), company_ciks=("320193",), observed_at=NOW)
    initial = CalendarSyncState(provider="fixture", revision=0, latest_run=run)
    success = CalendarSyncSuccess(run=run, completed_at=NOW, event_count=0, cancelled_count=0)
    complete = replace(initial, revision=1, full_success=success)
    get = {"TableName": "calendar", "Key": encode({"expected_date": SYNC_PARTITION, "cik": "fixture"}), "ConsistentRead": True}
    stub.add_response("get_item", {}, get)
    stub.add_response("put_item", {}, {"TableName": "calendar", "Item": encode(state_record(initial)),
        "ConditionExpression": "attribute_not_exists(#date)", "ExpressionAttributeNames": {"#date": "expected_date"}})
    stub.add_response("get_item", {"Item": encode(state_record(initial))}, get)
    stub.add_response("get_item", {"Item": encode(state_record(initial))}, get)
    stub.add_response("put_item", {}, {"TableName": "calendar", "Item": encode(state_record(complete)),
        "ConditionExpression": "attribute_exists(#date) AND #rev = :old",
        "ExpressionAttributeNames": {"#date": "expected_date", "#rev": "revision"},
        "ExpressionAttributeValues": encode({":old": 0})})
    stub.add_response("get_item", {"Item": encode(state_record(complete))}, get)
    async def scenario():
        repository = DynamoDBCalendarRepository(execution)
        assert await repository.begin_sync(run) == run
        assert await repository.begin_sync(run) == run
        await repository.complete_sync(success)
        await repository.complete_sync(success)
    asyncio.run(scenario())


def test_sdk_calendar_query_continues_past_tombstones_without_filter(sdk):
    _, stub, execution = sdk
    value = event()
    tombstone = {**record(value), "calendar_active": False}
    second = record(replace(value, company_cik="789019", ticker="MSFT"))
    key = encode({"expected_date": "2026-10-08", "cik": value.company_cik})
    query = {"TableName": "calendar", "KeyConditionExpression": "#pk = :pk",
        "ExpressionAttributeNames": {"#pk": "expected_date"},
        "ExpressionAttributeValues": encode({":pk": "2026-10-08"}), "ConsistentRead": True, "Limit": 100}
    stub.add_response("query", {"Items": [encode(tombstone)], "LastEvaluatedKey": key}, query)
    stub.add_response("query", {"Items": [encode(second)]}, {**query, "ExclusiveStartKey": key})
    result = asyncio.run(DynamoDBCalendarRepository(execution).get_events(NOW, NOW))
    assert len(result) == 1 and result[0].ticker == "MSFT"


def test_sdk_sync_permission_errors_are_not_missing_state(sdk):
    client, stub, execution = sdk
    stub.add_client_error("get_item", service_error_code="AccessDeniedException", expected_params={
        "TableName": "calendar", "Key": encode({"expected_date": SYNC_PARTITION, "cik": "fixture"}), "ConsistentRead": True})
    with pytest.raises(client.exceptions.ClientError):
        asyncio.run(DynamoDBCalendarRepository(execution).get_sync_state("fixture"))
