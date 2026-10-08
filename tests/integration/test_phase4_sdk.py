"""Validate S3/SNS/SQS wire contracts with the installed botocore model."""

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import boto3
from botocore.stub import Stubber
import pytest

from finbot_ingestion.aws_config import AWSIOConfig, MessagingConfig, StorageConfig
from finbot_ingestion.aws_execution import AWSExecution
from finbot_ingestion.domain import Artifact, Filing
from finbot_ingestion.domain.checkpoints import ArtifactCheckpoint
from finbot_ingestion.domain.events import ArtifactReady
from finbot_ingestion.domain.identity import artifact_key
from finbot_ingestion.messaging.dead_letter import SQSDeadLetterPublisher, WorkFailure
from finbot_ingestion.messaging.publisher import InvalidPublishResponse
from finbot_ingestion.messaging.sns_publisher import SNSArtifactEventPublisher
from finbot_ingestion.sec.client import DownloadedDocument
from finbot_ingestion.storage.errors import StorageConflict, StorageLimitError
from finbot_ingestion.storage.s3_artifact_store import S3ArtifactStore, provenance

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)
ACCESSION = "0000320193-26-000001"
ARTIFACT = Artifact(accession_number=ACCESSION, company_cik="320193", ticker="AAPL", form_type="8-K",
    filename="Primary.htm", sec_url=f"https://www.sec.gov/Archives/edgar/data/320193/{ACCESSION.replace('-', '')}/Primary.htm",
    discovered_at=NOW)
KEY = artifact_key(ARTIFACT.company_cik, ACCESSION, ARTIFACT.filename)
CONFIG = MessagingConfig("us-east-1", "arn:aws:sns:us-east-1:123456789012:artifacts",
                         "https://sqs.us-east-1.amazonaws.com/123456789012/failed")


@contextmanager
def sdk(service):
    client = boto3.client(service, region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
    with AWSExecution(client, AWSIOConfig("us-east-1")) as execution, Stubber(client) as stub:
        yield execution, stub
        stub.assert_no_pending_responses()


def head(**changes):
    return {"Metadata": {"finbot-provenance": json.dumps(provenance(ARTIFACT), ensure_ascii=True, sort_keys=True),
                         "finbot-content-type": '"text/html"'},
            "ContentLength": 3, "ContentType": "text/html", "LastModified": NOW, **changes}


def test_s3_conditional_put_head_and_exact_body():
    with sdk("s3") as (execution, stub):
        store = S3ArtifactStore(execution, StorageConfig("finbot-artifacts"))
        stub.add_response("put_object", {"ETag": '"opaque"'}, {
            "Bucket": "finbot-artifacts", "Key": KEY, "IfNoneMatch": "*", "Body": b"\x00\xff\r",
            "ContentType": "text/html", "Metadata": head()["Metadata"]})
        stub.add_response("head_object", head(), {"Bucket": "finbot-artifacts", "Key": KEY})
        stored = asyncio.run(store.put_if_absent(ARTIFACT, DownloadedDocument(b"\x00\xff\r", "text/html", ARTIFACT.sec_url)))
        assert stored.stored_at == NOW and stored.size_bytes == 3


@pytest.mark.parametrize("status,code", [(412, "PreconditionFailed"), (409, "ConditionalRequestConflict")])
def test_conditional_conflicts_reconcile_first_object(status, code):
    with sdk("s3") as (execution, stub):
        store = S3ArtifactStore(execution, StorageConfig("finbot-artifacts"))
        stub.add_client_error("put_object", service_error_code=code, http_status_code=status)
        stub.add_response("head_object", head(), {"Bucket": "finbot-artifacts", "Key": KEY})
        stored = asyncio.run(store.put_if_absent(ARTIFACT, DownloadedDocument(b"replacement", "text/plain", ARTIFACT.sec_url)))
        assert stored.content_type == "text/html" and stored.size_bytes == 3


def test_s3_missing_object_only_unambiguous_404():
    with sdk("s3") as (execution, stub):
        store = S3ArtifactStore(execution, StorageConfig("finbot-artifacts"))
        stub.add_client_error("head_object", service_error_code="404", http_status_code=404)
        assert asyncio.run(store.inspect(ARTIFACT)) is None
        stub.add_client_error("head_object", service_error_code="NoSuchBucket", http_status_code=404)
        with pytest.raises(execution.client.exceptions.NoSuchBucket):
            asyncio.run(store.inspect(ARTIFACT))


@pytest.mark.parametrize("changes", [
    {"Metadata": {}}, {"ContentLength": 1000}, {"ContentType": "different"},
    {"Metadata": {"finbot-provenance": "broken", "finbot-content-type": "null"}},
    {"Metadata": {"finbot-provenance": json.dumps({**provenance(ARTIFACT), "ticker": "WRONG"}), "finbot-content-type": "null"}},
    {"Metadata": {"finbot-provenance": json.dumps({**provenance(ARTIFACT), "version": True}), "finbot-content-type": "null"}},
])
def test_existing_unverifiable_object_fails_without_replacement(changes):
    with sdk("s3") as (execution, stub):
        store = S3ArtifactStore(execution, StorageConfig("finbot-artifacts", max_artifact_bytes=10))
        stub.add_response("head_object", head(**changes))
        with pytest.raises(StorageConflict):
            asyncio.run(store.inspect(ARTIFACT))


def test_storage_limit_fails_before_sdk_call():
    with sdk("s3") as (execution, stub):
        store = S3ArtifactStore(execution, StorageConfig("finbot-artifacts", max_artifact_bytes=2))
        with pytest.raises(StorageLimitError):
            asyncio.run(store.put_if_absent(ARTIFACT, DownloadedDocument(b"abc", None, ARTIFACT.sec_url)))


def event():
    from dataclasses import replace
    filing = Filing(accession_number=ACCESSION, company_cik="320193", ticker="AAPL", form_type="8-K",
                    filed_at=NOW, discovered_at=NOW, filing_index_url="https://www.sec.gov/index")
    return ArtifactReady.from_artifact(filing, replace(ARTIFACT, s3_uri="s3://finbot-artifacts/" + KEY, stored_at=NOW))


def test_sns_plain_json_message_and_acknowledgment():
    with sdk("sns") as (execution, stub):
        stub.add_response("publish", {"MessageId": "message-1"}, {"TopicArn": CONFIG.topic_arn, "Message": event().to_json()})
        asyncio.run(SNSArtifactEventPublisher(execution, CONFIG).publish_artifact_ready(event()))
        stub.add_response("publish", {}, {"TopicArn": CONFIG.topic_arn, "Message": event().to_json()})
        with pytest.raises(InvalidPublishResponse):
            asyncio.run(SNSArtifactEventPublisher(execution, CONFIG).publish_artifact_ready(event()))


def test_sqs_versioned_metadata_envelope_and_acknowledgment():
    progress = ArtifactCheckpoint(artifact_id=ARTIFACT.artifact_id, acquisition_failures=1,
        failure_stage="ACQUIRE", failure_error="broken\x00source", failure_at=NOW, terminal_stage="ACQUIRE",
        terminal_error="broken\x00source", terminal_error_type="SECDataError", terminal_at=NOW)
    failure = WorkFailure.from_checkpoint(ARTIFACT, progress)
    payload = json.loads(failure.message)
    assert payload["schema_version"] == "1.0" and "content" not in payload
    assert "\x00" not in failure.message and payload["error"] == "broken\x00source"
    with sdk("sqs") as (execution, stub):
        stub.add_response("send_message", {"MessageId": "message-1"}, {
            "QueueUrl": CONFIG.dead_letter_queue_url, "MessageBody": failure.message})
        asyncio.run(SQSDeadLetterPublisher(execution, CONFIG).send(failure))


def test_explicit_client_factory_uses_finite_policy_and_closes():
    captured = {}
    client = SimpleNamespace(close=lambda: captured.update(closed=True))
    def create(service, **params):
        captured.update(service=service, **params)
        return client
    with AWSExecution.from_config("s3", AWSIOConfig("us-east-1"), session=SimpleNamespace(client=create)):
        assert captured["service"] == "s3"
        assert captured["config"].retries == {"mode": "standard", "total_max_attempts": 3}
        assert captured["config"].max_pool_connections == 2
    assert captured["closed"]
