"""Reusable offline ingestion test support."""

from collections import deque
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import asyncio
import json

import boto3
import pytest

from finbot_ingestion.aws_config import AWSIOConfig, StorageConfig, MessagingConfig
from finbot_ingestion.aws_execution import AWSExecution
from finbot_ingestion.execution import BlockingExecution
from finbot_ingestion.ingestion.artifact_downloader import ArtifactDownloader
from finbot_ingestion.ingestion.config import WorkflowConfig
from finbot_ingestion.ingestion.discovery_service import DiscoveryService
from finbot_ingestion.ingestion.ingestion_worker import IngestionWorker
from finbot_ingestion.ingestion.work_control import WorkControl
from finbot_ingestion.messaging.dead_letter import SQSDeadLetterPublisher
from finbot_ingestion.messaging.sns_publisher import SNSFilingEventPublisher
from finbot_ingestion.repositories.package_checkpoint import PackageCheckpoint
from finbot_ingestion.sec.client import DownloadedDocument
from finbot_ingestion.sec.filing_index import parse_filing_index
from finbot_ingestion.storage.s3_artifact_store import S3ArtifactStore

from tests.support.aws_fakes import MemoryS3, MemoryMessages
from tests.support.dynamodb import make_filing, make_artifact, NOW, ACCESSION


RAW = b"\x00SEC\xff\r\noriginal"
BUCKET = "finbot-artifacts"
MESSAGING = MessagingConfig("us-east-1", "arn:aws:sns:us-east-1:123456789012:filing-ready",
                            "https://sqs.us-east-1.amazonaws.com/123456789012/ingestion-failed")


class FakeSEC:
    def __init__(self):
        self.filings = [make_filing()]
        self.calls, self.failures = [], deque()

    def _call(self, operation):
        self.calls.append(operation)
        if self.failures and self.failures[0][0] == operation:
            _, error = self.failures.popleft()
            raise error

    def get_company_submissions(self, company):
        self._call("submissions")
        return self.filings

    def get_filing_index(self, filing):
        self._call("index")
        root = Path(__file__).parents[1] / "fixtures" / "sec_package"
        # Fixtures use the baseline accession; rewrite the directory for amendments.
        directory = json.loads((root / "directory.json").read_text())
        if filing.accession_number != ACCESSION:
            directory["directory"]["name"] = directory["directory"]["name"].replace(
                ACCESSION.replace("-", ""), filing.accession_number.replace("-", ""))
            for item in directory["directory"]["item"]:
                item["name"] = item["name"].replace(ACCESSION, filing.accession_number)
        html = (root / "index.html").read_bytes().replace(
            ACCESSION.replace("-", "").encode(), filing.accession_number.replace("-", "").encode())
        if filing.form_type == "8-K/A":
            html = html.replace(b">8-K<", b">8-K/A<")
        return parse_filing_index(filing, html, directory)

    def download_document(self, url, *, max_bytes, context=None):
        assert context["ticker"] and context["cik"] and context["accession_number"]
        self._call("download")
        assert len(RAW) <= max_bytes
        return DownloadedDocument(RAW, "application/octet-stream", url)


@pytest.fixture
def system(db, monkeypatch):
    with ExitStack() as stack:
        clients = {service: boto3.client(service, region_name="us-east-1", aws_access_key_id="testing",
                                        aws_secret_access_key="testing") for service in ("s3", "sns", "sqs")}
        s3, messages, sec = MemoryS3(), MemoryMessages(), FakeSEC()
        monkeypatch.setattr(clients["s3"], "_make_api_call", s3.api)
        for service in ("sns", "sqs"):
            monkeypatch.setattr(clients[service], "_make_api_call", messages.api)
        executions = {name: stack.enter_context(AWSExecution(client, AWSIOConfig("us-east-1", max_attempts=1)))
                      for name, client in clients.items()}
        sec_execution = stack.enter_context(BlockingExecution(max_workers=1))
        store = S3ArtifactStore(executions["s3"], StorageConfig(BUCKET))
        async def no_sleep(seconds):
            await asyncio.sleep(0)
        def worker(**changes):
            config = WorkflowConfig(checkpoint_attempts=1, dead_letter_attempts=1, **changes)
            control = WorkControl(config, sleeper=no_sleep, now=lambda: NOW + timedelta(seconds=2), random_value=lambda: 0)
            discovery = DiscoveryService(sec, sec_execution, db.filings, db.artifacts, control)
            return IngestionWorker(discovery, ArtifactDownloader(sec, sec_execution, store,
                max_artifact_bytes=64 * 1024 * 1024), SNSFilingEventPublisher(executions["sns"], MESSAGING),
                SQSDeadLetterPublisher(executions["sqs"], MESSAGING), db.filings, db.artifacts, control)
        yield SimpleNamespace(db=db, sec=sec, s3=s3, messages=messages, store=store, worker=worker)


async def seed(system, *, complete=True, filename="Primary.htm"):
    filing, child = make_filing(), make_artifact(filename)
    await system.db.filings.create_if_absent(filing)
    if complete:
        await PackageCheckpoint(system.db.filings, system.db.artifacts).persist(filing, [child],
            primary_document_name=filename, completed_at=NOW)
    else:
        await system.db.artifacts.create_if_absent(child)
    return child


def run(coro):
    return asyncio.run(coro)
