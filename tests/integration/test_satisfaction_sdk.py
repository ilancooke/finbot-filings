"""Durable earnings satisfaction SDK wire contract."""

import asyncio
import json

from botocore.stub import Stubber
import boto3
import pytest

from finbot_ingestion.domain.satisfaction import EventSatisfaction
from finbot_ingestion.domain.sec_items import SECItemMetadata, FilingObservation
from finbot_ingestion.repositories.dynamodb import DynamoDBExecution, DynamoDBFilingRepository
from finbot_ingestion.repositories.dynamodb.satisfaction import (
    DynamoDBSatisfactionRepository,
    satisfaction_record,
    SATISFACTION_PARTITION,
)
from finbot_ingestion.repositories.dynamodb.serialization import encode, record
from finbot_ingestion.repositories.errors import RepositoryConflict
from finbot_ingestion.scheduler.earnings_satisfaction_policy import EarningsSatisfactionPolicy

from tests.support.calendar import event, APPLE
from tests.support.dynamodb import make_filing, NOW, ACCESSION, CONFIG


def test_satisfaction_sdk_wire_condition_strong_read_and_missing_parent():
    async def scenario():
        filing = make_filing()
        expected = event(time_of_day="after_market")
        window = EarningsSatisfactionPolicy().window(expected)
        observation = FilingObservation(filing, SECItemMetadata(ACCESSION, APPLE.cik, "known", ("2.02",)))
        decision = EarningsSatisfactionPolicy().evaluate(expected, filing, observation.sec_items)
        value = EventSatisfaction.from_match(expected, window, observation, decision, at=NOW)
        assert len(json.dumps(encode(satisfaction_record(value))).encode()) < 2048
        client = boto3.client("dynamodb", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
        with DynamoDBExecution(client, CONFIG) as execution, Stubber(client) as stub:
            filings = DynamoDBFilingRepository(execution)
            repo = DynamoDBSatisfactionRepository(execution, filings)
            parent = {**record(filing), "revision": 0, "retry_count": 0, "pending_work_kind": "ENUMERATE",
                "pending_work_sort": filing.discovered_at.isoformat(timespec="microseconds").replace("+00:00", "Z") + "/" + ACCESSION}
            get = {"TableName": "filings", "Key": encode({"accession_number": ACCESSION}), "ConsistentRead": True}
            stub.add_response("get_item", {"Item": encode(parent)}, get)
            stub.add_response("put_item", {}, {"TableName": "calendar", "Item": encode(satisfaction_record(value)),
                "ConditionExpression": "attribute_not_exists(#key)", "ExpressionAttributeNames": {"#key": "expected_date"}})
            assert await repo.create_if_absent(value)
            stub.add_response("get_item", {"Item": encode(satisfaction_record(value))}, {"TableName": "calendar",
                "Key": encode({"expected_date": SATISFACTION_PARTITION, "cik": value.identity.key}), "ConsistentRead": True})
            assert await repo.get(value.identity) == value
            stub.add_response("get_item", {}, get)
            with pytest.raises(RepositoryConflict):
                await repo.create_if_absent(value)
            stub.assert_no_pending_responses()
    asyncio.run(scenario())
