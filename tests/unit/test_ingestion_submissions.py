import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from finbot_ingestion.domain import Company
from finbot_ingestion.domain.identity import SUPPORTED_FORMS
from finbot_ingestion.sec.submissions import SECDataError, parse_company_submissions, parse_filing_arrays

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)
COMPANY = Company(ticker="aapl", cik="320193", name="Apple")


@pytest.fixture
def payload():
    path = Path(__file__).parents[1] / "fixtures" / "submissions_mixed.json"
    return json.loads(path.read_text())


def test_all_forms_amendments_deduplication_and_source_time(payload):
    filings = parse_company_submissions(COMPANY, payload, discovered_at=NOW)
    assert len(filings) == 6  # No legacy default count=5 cutoff.
    assert {f.form_type for f in filings} == SUPPORTED_FORMS
    assert [f.accession_number for f in filings] == [
        f"0000320193-26-{number:06d}" for number in range(6, 0, -1)
    ]
    assert filings[-1].filed_at == datetime(2026, 10, 1, 20, tzinfo=timezone.utc)
    assert all(f.discovered_at == NOW and f.company_cik == "0000320193" for f in filings)
    assert filings[0].primary_document_name is None
    assert filings[1].primary_document_name is None


def test_optional_columns_can_be_absent(payload):
    recent = payload["filings"]["recent"]
    for name in ("primaryDocument", "reportDate", "filingDate"):
        del recent[name]
    filings = parse_filing_arrays(COMPANY, recent, discovered_at=NOW)
    assert len(filings) == 6
    assert all(f.primary_document_name is None for f in filings)


@pytest.mark.parametrize("timestamp", [None, "", "2026-10-01", "2026-10-01T20:00:00", "invalid", 123])
def test_never_fabricates_acceptance_time(payload, timestamp):
    payload["filings"]["recent"]["acceptanceDateTime"][0] = timestamp
    with pytest.raises(SECDataError, match="index 0"):
        parse_company_submissions(COMPANY, payload, discovered_at=NOW)


@pytest.mark.parametrize("column", ["accessionNumber", "form", "acceptanceDateTime", "primaryDocument", "reportDate", "filingDate"])
def test_misaligned_columns_are_rejected(payload, column):
    payload["filings"]["recent"][column].pop()
    with pytest.raises(SECDataError, match="inconsistent lengths"):
        parse_company_submissions(COMPANY, payload, discovered_at=NOW)


@pytest.mark.parametrize("column", ["accessionNumber", "form", "acceptanceDateTime"])
def test_required_columns_are_not_optional(payload, column):
    del payload["filings"]["recent"][column]
    with pytest.raises(SECDataError, match=column):
        parse_company_submissions(COMPANY, payload, discovered_at=NOW)


def test_conflicting_duplicate_cannot_silently_replace_history(payload):
    payload["filings"]["recent"]["form"][6] = "10-K"
    with pytest.raises(SECDataError, match="conflicting metadata"):
        parse_company_submissions(COMPANY, payload, discovered_at=NOW)


@pytest.mark.parametrize("bad", [None, [], {}, {"filings": []}, {"filings": {"recent": []}}])
def test_invalid_response_shape(bad):
    with pytest.raises(SECDataError):
        parse_company_submissions(COMPANY, bad, discovered_at=NOW)


def test_response_cannot_be_assigned_to_wrong_company(payload):
    payload["cik"] = "123"
    with pytest.raises(SECDataError, match="CIK"):
        parse_company_submissions(COMPANY, payload, discovered_at=NOW)


def test_empty_submission_arrays_are_valid():
    assert parse_filing_arrays(COMPANY, {
        "accessionNumber": [], "form": [], "acceptanceDateTime": [],
    }, discovered_at=NOW) == []


@pytest.mark.parametrize("column,value", [
    ("primaryDocument", "../filing.htm"),
    ("primaryDocument", 123),
    ("accessionNumber", "000032019326000001"),
    ("form", None),
])
def test_invalid_relevant_identity_fields(payload, column, value):
    payload["filings"]["recent"][column][0] = value
    with pytest.raises(SECDataError, match="index 0"):
        parse_company_submissions(COMPANY, payload, discovered_at=NOW)


def test_scalar_optional_column_is_rejected(payload):
    payload["filings"]["recent"]["primaryDocument"] = "filing.htm"
    with pytest.raises(SECDataError, match="primaryDocument must be an array"):
        parse_company_submissions(COMPANY, payload, discovered_at=NOW)


def test_repeated_parsing_observes_new_response_without_cache(payload):
    first = parse_company_submissions(COMPANY, payload, discovered_at=NOW)
    payload["filings"]["recent"]["accessionNumber"][5] = "0000320193-26-000099"
    second = parse_company_submissions(COMPANY, payload, discovered_at=NOW)
    assert first[0].accession_number.endswith("000006")
    assert second[0].accession_number.endswith("000099")
