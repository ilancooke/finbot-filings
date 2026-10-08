"""Validate wire parameters using the actual botocore service model and Stubber."""

import asyncio
from datetime import datetime, timezone
from dataclasses import replace

import boto3
from botocore.stub import Stubber
import pytest

from finbot_ingestion.domain import Artifact, Filing
from finbot_ingestion.repositories.dynamodb import DynamoDBArtifactRepository, DynamoDBConfig, DynamoDBExecution, DynamoDBFilingRepository
from finbot_ingestion.repositories.dynamodb.serialization import encode, pending_sort, record

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)
ACCESSION = "0000320193-26-000001"


@pytest.fixture
def sdk():
    client = boto3.client("dynamodb", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
    config = DynamoDBConfig("us-east-1", "companies", "calendar", "filings", "artifacts")
    with Stubber(client) as stub, DynamoDBExecution(client, config) as execution:
        yield client, stub, execution
        stub.assert_no_pending_responses()


def test_sdk_conditional_creation_and_duplicate(sdk):
    client, stub, execution = sdk
    filing = Filing(accession_number=ACCESSION, company_cik="320193", ticker="AAPL", form_type="8-K",
                    filed_at=NOW, discovered_at=NOW, filing_index_url="https://www.sec.gov/index")
    item = {**record(filing), "revision": 0, "retry_count": 0, "pending_work_kind": "ENUMERATE",
            "pending_work_sort": pending_sort(NOW, ACCESSION)}
    params = {"TableName": "filings", "Item": encode(item),
              "ConditionExpression": "attribute_not_exists(#key)",
              "ExpressionAttributeNames": {"#key": "accession_number"}}
    stub.add_response("put_item", {}, params)
    stub.add_client_error("put_item", service_error_code="ConditionalCheckFailedException", expected_params=params)
    stub.add_response("get_item", {"Item": encode(item)}, {
        "TableName": "filings", "Key": {"accession_number": {"S": ACCESSION}}, "ConsistentRead": True})
    async def scenario():
        repo = DynamoDBFilingRepository(execution)
        assert await repo.create_if_absent(filing)
        assert not await repo.create_if_absent(filing)
    asyncio.run(scenario())


def test_sdk_storage_checkpoint_atomic_update(sdk):
    client, stub, execution = sdk
    artifact = Artifact(accession_number=ACCESSION, company_cik="320193", ticker="AAPL", form_type="8-K",
                        filename="Primary.htm", sec_url="https://www.sec.gov/doc", discovered_at=NOW)
    initial = {**record(artifact), "revision": 0, "pending_work_kind": "ACQUIRE",
               "pending_work_sort": pending_sort(NOW, artifact.artifact_id)}
    key = {"artifact_id": {"S": artifact.artifact_id}}
    stub.add_response("get_item", {"Item": encode(initial)}, {"TableName": "artifacts", "Key": key, "ConsistentRead": True})
    stored = {**initial, **record(replace(artifact, s3_uri="s3://artifacts/key", stored_at=NOW)),
              "revision": 1, "pending_work_kind": "PUBLISH"}
    stub.add_response("update_item", {"Attributes": encode(stored)}, {
        "TableName": "artifacts", "Key": key,
        "ConditionExpression": "attribute_exists(#key) AND #rev = :old",
        "UpdateExpression": "SET #rev = :new, #s0 = :s0, #s1 = :s1, #s2 = :s2 REMOVE #r0, #r1",
        "ExpressionAttributeNames": {"#key": "artifact_id", "#rev": "revision", "#s0": "s3_uri",
            "#s1": "stored_at", "#s2": "pending_work_kind", "#r0": "content_type", "#r1": "size_bytes"},
        "ExpressionAttributeValues": {":old": {"N": "0"}, ":new": {"N": "1"},
            ":s0": {"S": "s3://artifacts/key"}, ":s1": {"S": "2026-10-08T00:00:00.000000Z"}, ":s2": {"S": "PUBLISH"}},
        "ReturnValues": "ALL_NEW",
    })
    asyncio.run(DynamoDBArtifactRepository(execution).mark_stored(artifact.artifact_id, "s3://artifacts/key", NOW, None, None))


def test_sdk_nonconditional_error_not_swallowed(sdk):
    client, stub, execution = sdk
    stub.add_client_error("get_item", service_error_code="ResourceNotFoundException", expected_params={
        "TableName": "filings", "Key": {"accession_number": {"S": ACCESSION}}, "ConsistentRead": True})
    with pytest.raises(client.exceptions.ResourceNotFoundException):
        asyncio.run(DynamoDBFilingRepository(execution).get(ACCESSION))


def test_sdk_paginator_exact_cursor_and_no_strong_gsi_read(sdk):
    _, stub, execution = sdk
    key = {"accession_number": {"S": ACCESSION}, "pending_work_kind": {"S": "ENUMERATE"},
           "pending_work_sort": {"S": "2020-01-01T00:00:00.000000Z/" + ACCESSION}}
    query = {"TableName": "filings", "IndexName": "PendingFilingEnumeration",
             "KeyConditionExpression": "#pk = :pk", "ExpressionAttributeNames": {"#pk": "pending_work_kind"},
             "ExpressionAttributeValues": {":pk": {"S": "ENUMERATE"}}, "Limit": 1}
    stub.add_response("query", {"Items": [key], "LastEvaluatedKey": key}, query)
    # A vanished index candidate produces an empty actionable page with a cursor.
    stub.add_response("get_item", {}, {"TableName": "filings", "Key": {"accession_number": {"S": ACCESSION}}, "ConsistentRead": True})
    stub.add_response("query", {"Items": []}, {**query, "ExclusiveStartKey": key})
    async def scenario():
        repo = DynamoDBFilingRepository(execution)
        first = await repo.list_pending(page_size=1)
        assert not first.items and first.next_token
        last = await repo.list_pending(page_size=1, token=first.next_token)
        assert not last.items and last.next_token is None
    asyncio.run(scenario())
