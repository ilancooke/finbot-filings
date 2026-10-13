"""Reusable offline dynamodb test support."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import asyncio

import boto3
import pytest

from finbot_ingestion.domain import Artifact, Filing
from finbot_ingestion.repositories.dynamodb import (
    DynamoDBArtifactRepository,
    DynamoDBCalendarRepository,
    DynamoDBCompanyRepository,
    DynamoDBConfig,
    DynamoDBExecution,
    DynamoDBFilingRepository,
)
from finbot_ingestion.sec.urls import document_url, filing_index_url

from tests.support.aws_fakes import MemoryAWS


NOW = datetime(2026, 10, 8, 20, tzinfo=timezone.utc)
ACCESSION = "0000320193-26-000001"
CONFIG = DynamoDBConfig("us-east-1", "companies", "calendar", "filings", "artifacts")


def make_filing(**changes):
    return replace(Filing(accession_number=ACCESSION, company_cik="320193", ticker="AAPL",
                          form_type="8-K", filed_at=NOW - timedelta(seconds=10), discovered_at=NOW,
                          filing_index_url=filing_index_url("320193", ACCESSION)), **changes)


def make_artifact(filename="Primary.htm", **changes):
    return replace(Artifact(accession_number=ACCESSION, company_cik="320193", ticker="AAPL",
                            form_type="8-K", filename=filename, discovered_at=NOW,
                            sec_url=document_url("320193", ACCESSION, filename)), **changes)


@pytest.fixture
def db(monkeypatch):
    client = boto3.client("dynamodb", region_name="us-east-1", aws_access_key_id="testing",
                          aws_secret_access_key="testing")
    memory = MemoryAWS(client)
    monkeypatch.setattr(client, "_make_api_call", memory.api)
    with DynamoDBExecution(client, CONFIG) as execution:
        yield SimpleNamespace(memory=memory, client=client, execution=execution,
            filings=DynamoDBFilingRepository(execution), artifacts=DynamoDBArtifactRepository(execution),
            companies=DynamoDBCompanyRepository(execution), calendar=DynamoDBCalendarRepository(execution))


def run(coro):
    return asyncio.run(coro)
