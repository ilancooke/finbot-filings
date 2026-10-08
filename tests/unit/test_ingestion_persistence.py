"""Pure configuration and serialization checks, without client construction."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from finbot_ingestion.config import ConfigurationError, IngestionConfig
from finbot_ingestion.domain import Artifact, ArtifactReady, Company, ExpectedEarningsEvent, Filing, FilingCheckpoint
from finbot_ingestion.repositories.dynamodb.config import DynamoDBConfig
from finbot_ingestion.repositories.dynamodb.serialization import decode, encode, pending_sort, record, restore, timestamp, validate_pending
from finbot_ingestion.repositories.errors import RepositoryDataError

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)
ACCESSION = "0000320193-26-000001"


def models():
    return [Company("AAPL", "320193", "Apple"),
        Filing(accession_number=ACCESSION, company_cik="320193", ticker="AAPL", form_type="8-K",
               filed_at=NOW, discovered_at=NOW, filing_index_url="https://www.sec.gov/index"),
        Artifact(accession_number=ACCESSION, company_cik="320193", ticker="AAPL", form_type="8-K",
                 filename="EX991.htm", discovered_at=NOW, sec_url="https://www.sec.gov/ex991"),
        ExpectedEarningsEvent(company_cik="320193", ticker="AAPL", expected_date=date(2026, 10, 8),
                              provider="fixture", synced_at=NOW,
                              raw_provider_payload={"decimal": 1.25, "nested": {"list": [True, None, "日本語"]}}),
        FilingCheckpoint(accession_number=ACCESSION),
        FilingCheckpoint(accession_number=ACCESSION, enumeration_completed_at=NOW,
                         resolved_primary_document_name="EX991.htm", enumerated_artifact_count=1),
    ]


@pytest.mark.parametrize("model", models())
def test_typed_round_trip(model):
    native = record(model)
    assert restore(type(model), decode(encode(native))) == model
    assert all(value is not None for value in native.values())
    if "discovered_at" in native:
        assert native["discovered_at"] == "2026-10-08T00:00:00.000000Z"
    if isinstance(model, Artifact):
        assert native["artifact_id"] == ACCESSION + "/EX991.htm"


def test_storage_and_ready_contract_unchanged():
    filing, artifact = models()[1:3]
    stored = replace(artifact, s3_uri="s3://artifacts/key", stored_at=NOW, size_bytes=123,
                     published_at=NOW, retry_count=2, last_error="earlier", last_error_at=NOW)
    restored = restore(Artifact, decode(encode(record(stored))))
    assert restored == stored
    assert type(restored.size_bytes) is int
    assert ArtifactReady.from_artifact(filing, restored).to_dict()["stored_at"] == "2026-10-08T00:00:00Z"
    assert "enumeration_completed_at" not in ArtifactReady.from_artifact(filing, restored).to_dict()


@pytest.mark.parametrize("changes", [
    {"repository_schema_version": 2}, {"repository_schema_version": True},
    {"artifact_id": "wrong"}, {"size_bytes": Decimal("1.5")}, {"retry_count": True},
    {"stored_at": "2026-10-08T00:00:00.000000Z"}, {"published_at": "2026-10-08T00:00:00.000000Z"},
    {"discovered_at": "2026-10-08T00:00:00"}, {"discovered_at": "2026-10-08T00:00:00Z"},
    {"stored_at": None}, {"last_error": "missing time"}, {"sec_url": 123},
])
def test_corrupt_artifact_records_fail(changes):
    with pytest.raises(RepositoryDataError):
        restore(Artifact, {**record(models()[2]), **changes})


@pytest.mark.parametrize("payload", [{"bad": float("nan")}, {"bad": float("inf")}, {"bad": object()}])
def test_invalid_provider_payload_rejected(payload):
    with pytest.raises(RepositoryDataError):
        record(replace(models()[3], raw_provider_payload=payload))


def test_sdk_float_and_oversized_item_rejected():
    with pytest.raises(RepositoryDataError, match="Float"):
        encode({"number": 1.25})
    with pytest.raises(RepositoryDataError, match="400 KB"):
        encode({"text": "é" * (210 * 1024)})
    assert encode({"integer": 2, "decimal": Decimal("1.25")})["decimal"] == {"N": "1.25"}


def test_fixed_width_sort_preserves_subsecond_order_and_timezone():
    later = NOW + timedelta(microseconds=1)
    assert pending_sort(NOW, "a") < pending_sort(later, "a")
    assert timestamp(NOW.astimezone(timezone(timedelta(hours=5)))) == timestamp(NOW)
    with pytest.raises(ValueError):
        timestamp(NOW.replace(tzinfo=None))


def test_gsi_key_limits_fail_without_truncating_identity():
    artifact = replace(models()[2], filename="é" * 510)
    item = {**record(artifact), "pending_work_kind": "ACQUIRE",
            "pending_work_sort": pending_sort(NOW, artifact.artifact_id)}
    with pytest.raises(RepositoryDataError, match="1024"):
        validate_pending(item, "ACQUIRE", artifact.artifact_id)
    assert artifact.filename == "é" * 510


@pytest.mark.parametrize("changes", [
    {"enumeration_completed_at": NOW}, {"resolved_primary_document_name": "x.htm"},
    {"enumeration_completed_at": NOW, "resolved_primary_document_name": "x.htm", "enumerated_artifact_count": 0},
    {"retry_count": True}, {"last_error": "without time"},
])
def test_invalid_filing_checkpoint(changes):
    with pytest.raises(ValueError):
        FilingCheckpoint(accession_number=ACCESSION, **changes)


def test_separate_configuration_no_credential_resolution(monkeypatch):
    import boto3
    def forbidden(*args, **kwargs):
        raise AssertionError("configuration must not create a Session")
    monkeypatch.setattr(boto3, "Session", forbidden)
    assert IngestionConfig.from_env({"SEC_USER_AGENT": "Finbot owner@example.com"})
    env = {"AWS_REGION": "us-east-1", "COMPANIES_TABLE": "companies", "CALENDAR_TABLE": "calendar",
           "FILINGS_TABLE": "filings", "ARTIFACTS_TABLE": "artifacts", "DYNAMODB_PAGE_SIZE": "25"}
    config = DynamoDBConfig.from_env(env)
    assert config.page_size == 25 and config.max_workers == 4
    for key, value in (("ARTIFACTS_TABLE", ""), ("DYNAMODB_PAGE_SIZE", "0"),
                       ("DYNAMODB_PAGE_SIZE", "1001"), ("DYNAMODB_MAX_ATTEMPTS", "1.5"),
                       ("DYNAMODB_READ_TIMEOUT_SECONDS", "nan"), ("ARTIFACTS_TABLE", "filings")):
        with pytest.raises(ConfigurationError):
            DynamoDBConfig.from_env({**env, key: value})
