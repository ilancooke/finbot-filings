"""Reusable offline dynamodb sdk test support."""

from botocore.stub import Stubber
import boto3
import pytest

from finbot_ingestion.repositories.dynamodb import DynamoDBConfig, DynamoDBExecution


@pytest.fixture
def sdk():
    client = boto3.client("dynamodb", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
    config = DynamoDBConfig("us-east-1", "companies", "calendar", "filings", "artifacts")
    with Stubber(client) as stub, DynamoDBExecution(client, config) as execution:
        yield client, stub, execution
        stub.assert_no_pending_responses()
