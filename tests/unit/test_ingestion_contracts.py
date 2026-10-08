import json
from dataclasses import FrozenInstanceError, fields, replace
from datetime import date, datetime, timedelta, timezone

import pytest

from finbot_ingestion.domain import Artifact, ArtifactReady, Company, ExpectedEarningsEvent, Filing
from finbot_ingestion.domain.identity import artifact_identity, artifact_key, normalize_cik
from finbot_ingestion.sec.urls import company_submissions_url, document_url, filing_index_url

ACCESSION = "0000320193-26-000001"
NOW = datetime(2026, 10, 8, 20, tzinfo=timezone.utc)


@pytest.fixture
def filing():
    return Filing(
        accession_number=ACCESSION, company_cik="320193", ticker="aapl",
        form_type="8-k", filed_at=NOW - timedelta(seconds=10), discovered_at=NOW,
        filing_index_url=filing_index_url("320193", ACCESSION),
    )


@pytest.fixture
def artifact():
    return Artifact(
        accession_number=ACCESSION, company_cik="320193", ticker="aapl",
        form_type="8-k", filename="ex991.htm", document_type="EX-99.1",
        sec_url=document_url("320193", ACCESSION, "ex991.htm"), discovered_at=NOW,
    )


@pytest.mark.parametrize("cik", ["320193", "0000320193", 320193])
def test_canonical_identity_and_endpoint_paths(cik):
    assert normalize_cik(cik) == "0000320193"
    assert company_submissions_url(cik).endswith("/CIK0000320193.json")
    assert filing_index_url(cik, ACCESSION) == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/"
        f"{ACCESSION}-index.html"
    )
    assert artifact_key(cik, ACCESSION, "ex991.htm") == f"0000320193/{ACCESSION}/ex991.htm"


@pytest.mark.parametrize("cik", [0, -1, True, 1.2, None, "", "１２３", "12345678901", "1.0"])
def test_invalid_cik(cik):
    with pytest.raises(ValueError, match="CIK"):
        normalize_cik(cik)


@pytest.mark.parametrize("filename", ["", ".", "..", "../x.htm", "/x.htm", "a/b.htm", "a\\b.htm", "%2e%2e", "%2fetc", "x\x00.htm", " x.htm"])
def test_unsafe_names_rejected_for_both_identity_and_urls(filename):
    with pytest.raises(ValueError):
        artifact_identity(ACCESSION, filename)
    with pytest.raises(ValueError):
        document_url("320193", ACCESSION, filename)


def test_identity_preserves_original_filename_and_excludes_ticker(artifact):
    assert artifact.artifact_id == f"{ACCESSION}/ex991.htm"
    assert replace(artifact, ticker="OTHER").artifact_id == artifact.artifact_id
    assert replace(artifact, filename="EX991.htm").artifact_id != artifact.artifact_id
    assert replace(artifact, accession_number="0000320193-26-000002").artifact_id != artifact.artifact_id
    assert document_url("320193", ACCESSION, "a#b.htm").endswith("/a%23b.htm")


def test_immutable_filing_and_aware_timestamps(filing):
    with pytest.raises(FrozenInstanceError):
        filing.form_type = "10-K"
    local = NOW.astimezone(timezone(timedelta(hours=-4)))
    assert replace(filing, filed_at=local).filed_at == NOW
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(filing, filed_at=NOW.replace(tzinfo=None))


def test_calendar_is_separate_from_filing_history():
    company = Company(" aapl ", "320193", "Apple", enabled=False)
    assert company.cik == "0000320193" and company.ticker == "AAPL"
    assert company.enabled is False
    event = ExpectedEarningsEvent(
        company_cik=company.cik, ticker=company.ticker, expected_date=date(2026, 10, 9),
        provider="fixture", synced_at=NOW, time_of_day="unknown",
    )
    assert replace(event, expected_date=date(2026, 10, 10)).expected_date != event.expected_date
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(event, provider_updated_at=NOW.replace(tzinfo=None))


def test_ready_event_requires_stored_artifact(filing, artifact):
    with pytest.raises(ValueError, match="stored artifact"):
        ArtifactReady.from_artifact(filing, artifact)
    with pytest.raises(ValueError, match="together"):
        replace(artifact, stored_at=NOW)
    with pytest.raises(ValueError, match="publication requires"):
        replace(artifact, published_at=NOW)


def test_ready_wire_contract_and_amendment_history(filing, artifact):
    stored = replace(artifact, s3_uri=f"s3://artifacts/0000320193/{ACCESSION}/ex991.htm", stored_at=NOW)
    event = ArtifactReady.from_artifact(filing, stored)
    assert json.loads(event.to_json()) == {
        "event_type": "artifact.ready", "schema_version": "1.0",
        "artifact_id": f"{ACCESSION}/ex991.htm", "filing_id": ACCESSION,
        "cik": "0000320193", "ticker": "AAPL", "form_type": "8-K",
        "document_type": "EX-99.1", "filename": "ex991.htm", "s3_uri": stored.s3_uri,
        "filed_at": "2026-10-08T19:59:50Z", "discovered_at": "2026-10-08T20:00:00Z",
        "stored_at": "2026-10-08T20:00:00Z",
    }
    amendment = replace(filing, accession_number="0000320193-26-000002", form_type="8-K/A")
    with pytest.raises(ValueError, match="accession_number"):
        ArtifactReady.from_artifact(amendment, stored)
    with pytest.raises(ValueError, match="artifact_id"):
        replace(event, artifact_id="made-up")
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(event, stored_at=None)
    with pytest.raises(ValueError, match="S3 object URI"):
        replace(event, s3_uri="https://example.test/doc")
    assert filing.form_type == "8-K" and filing.accession_number == ACCESSION


def test_artifact_contract_has_only_durable_fields(artifact):
    names = {f.name for f in fields(artifact)}
    assert not names.intersection({"status", "downloading", "content", "sha256", "content_hash"})
    with pytest.raises(ValueError, match="nonnegative"):
        replace(artifact, retry_count=-1)


@pytest.mark.parametrize("count", [None, True, 1.5])
def test_retry_count_must_be_an_integer(artifact, count):
    with pytest.raises(ValueError, match="retry_count"):
        replace(artifact, retry_count=count)
