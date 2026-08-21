from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from finbot_filings.models import Company, Filing
from finbot_filings.sec.client import SECDataError
from finbot_filings.sec.filings import (
    SUBMISSIONS_URL_TEMPLATE,
    TICKER_MAPPING_URL,
    TickerNotFoundError,
    discover_filings,
    filing_index_url,
    historical_submissions_url,
    normalize_accession_number,
    normalize_ticker,
    parse_recent_filings,
    primary_document_url,
    resolve_company,
    select_filings,
    validate_count,
    validate_form,
)


class StubClient:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.requested: list[str] = []

    def get_json(self, url: str) -> Any:
        self.requested.append(url)
        return self.responses[url]


def recent_payload() -> dict[str, list[str]]:
    return {
        "accessionNumber": [
            "0000320193-24-000100",
            "0000320193-25-000079",
            "0000320193-25-000081",
            "0000320193-23-000106",
            "0000320193-25-000050",
        ],
        "filingDate": [
            "2024-11-01",
            "2025-10-31",
            "2025-11-03",
            "2023-11-03",
            "2025-07-25",
        ],
        # Deliberately makes the older filing's report date newer to prove it is
        # not used for ordering.
        "reportDate": [
            "2024-09-28",
            "2025-09-27",
            "2025-09-27",
            "2026-09-26",
            "2025-06-28",
        ],
        "form": ["10-K", "10-K", "10-K/A", "10-K", "10-Q"],
        "primaryDocument": [
            "aapl-20240928.htm",
            "aapl-20250927.htm",
            "aapl-20250927x10ka.htm",
            "aapl-20230930.htm",
            "aapl-20250628.htm",
        ],
    }


def test_normalize_ticker_is_case_insensitive() -> None:
    assert normalize_ticker(" aApL ") == "AAPL"


def test_resolve_company_from_official_mapping() -> None:
    client = StubClient(
        {
            TICKER_MAPPING_URL: {
                "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}
            }
        }
    )
    assert resolve_company(client, "aapl") == Company("AAPL", "Apple Inc.", 320193)


def test_resolve_company_rejects_unknown_ticker() -> None:
    client = StubClient({TICKER_MAPPING_URL: {}})
    with pytest.raises(TickerNotFoundError, match="NOPE"):
        resolve_company(client, "nope")


def test_parse_recent_converts_parallel_arrays(company: Company) -> None:
    filings = parse_recent_filings(company, recent_payload())
    assert len(filings) == 5
    assert filings[1].filing_date == date(2025, 10, 31)
    assert filings[1].report_date == date(2025, 9, 27)
    assert filings[1].primary_document == "aapl-20250927.htm"
    assert filings[1].company_name == "Apple Inc."


def test_parse_recent_rejects_misaligned_parallel_arrays(company: Company) -> None:
    payload = recent_payload()
    payload["form"] = payload["form"][:-1]
    with pytest.raises(SECDataError, match="inconsistent lengths"):
        parse_recent_filings(company, payload)


def test_exact_10k_selection_excludes_amendment_and_sorts_by_filing_date(
    company: Company,
) -> None:
    selected = select_filings(parse_recent_filings(company, recent_payload()), "10-K", 10)
    assert [filing.form for filing in selected] == ["10-K", "10-K", "10-K"]
    assert [filing.accession_number for filing in selected] == [
        "0000320193-25-000079",
        "0000320193-24-000100",
        "0000320193-23-000106",
    ]
    assert selected[-1].report_date == date(2026, 9, 26)


def test_exact_10q_selection_excludes_amendment_and_applies_count(
    company: Company,
) -> None:
    payload = recent_payload()
    payload["accessionNumber"].append("0000320193-25-000051")
    payload["filingDate"].append("2025-07-28")
    payload["reportDate"].append("2025-06-28")
    payload["form"].append("10-Q/A")
    payload["primaryDocument"].append("aapl-20250628x10qa.htm")
    selected = select_filings(parse_recent_filings(company, payload), "10-Q", 1)
    assert len(selected) == 1
    assert selected[0].form == "10-Q"


@pytest.mark.parametrize("count", [0, -1])
def test_invalid_count(count: int) -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        validate_count(count)


@pytest.mark.parametrize("form", ["8-K", "10-K/A", ""])
def test_unsupported_form(form: str) -> None:
    with pytest.raises(ValueError, match="supported forms"):
        validate_form(form)


def test_accession_normalization() -> None:
    assert normalize_accession_number("0000320193-25-000079") == "000032019325000079"
    with pytest.raises(ValueError, match="invalid SEC accession"):
        normalize_accession_number("000032019325000079")


def test_sec_url_construction_uses_numeric_cik_and_normalized_path() -> None:
    accession = "0000320193-25-000079"
    base = "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079"
    assert filing_index_url(320193, accession) == (
        f"{base}/0000320193-25-000079-index.html"
    )
    assert primary_document_url(320193, accession, "aapl-20250927.htm") == (
        f"{base}/aapl-20250927.htm"
    )


def test_historical_submissions_url_is_centralized_and_validated() -> None:
    filename = "CIK0000019617-submissions-006.json"
    assert historical_submissions_url(filename) == (
        f"https://data.sec.gov/submissions/{filename}"
    )
    with pytest.raises(ValueError, match="invalid SEC historical"):
        historical_submissions_url("../other.json")


def test_discover_uses_zero_padded_submissions_endpoint(company: Company) -> None:
    submissions_url = SUBMISSIONS_URL_TEMPLATE.format(cik=company.cik)
    client = StubClient(
        {
            TICKER_MAPPING_URL: {
                "0": {"cik_str": company.cik, "ticker": "AAPL", "title": company.name}
            },
            submissions_url: {
                "filings": {
                    "recent": recent_payload(),
                    "files": [
                        {
                            "name": "CIK0000320193-submissions-001.json",
                            "filingFrom": "2020-01-01",
                            "filingTo": "2020-12-31",
                        }
                    ],
                }
            },
        }
    )
    resolved, filings = discover_filings(client, "aapl", "10-K", 2)
    assert resolved == company
    assert len(filings) == 2
    assert client.requested == [TICKER_MAPPING_URL, submissions_url]
    assert submissions_url.endswith("CIK0000320193.json")


def test_discover_loads_historical_files_newest_first_until_count(
    company: Company,
) -> None:
    submissions_url = SUBMISSIONS_URL_TEMPLATE.format(cik=company.cik)
    newer_name = "CIK0000320193-submissions-001.json"
    older_name = "CIK0000320193-submissions-002.json"
    newer_url = historical_submissions_url(newer_name)
    older_url = historical_submissions_url(older_name)

    recent = {
        "accessionNumber": ["0000320193-26-000001"],
        "filingDate": ["2026-02-01"],
        "reportDate": ["2025-12-31"],
        "form": ["10-K"],
        "primaryDocument": ["aapl-20251231.htm"],
    }
    newer_history = {
        "accessionNumber": [
            "0000320193-26-000001",
            "0000320193-25-000002",
            "0000320193-25-000003",
        ],
        "filingDate": ["2026-02-01", "2025-02-01", "2025-02-02"],
        "reportDate": ["2025-12-31", "2024-12-31", "2024-12-31"],
        "form": ["10-K", "10-K", "10-K/A"],
        "primaryDocument": [
            "aapl-20251231.htm",
            "aapl-20241231.htm",
            "aapl-20241231x10ka.htm",
        ],
    }
    older_history = {
        "accessionNumber": ["0000320193-24-000004"],
        "filingDate": ["2024-02-01"],
        "reportDate": ["2026-12-31"],
        "form": ["10-K"],
        "primaryDocument": ["aapl-20231231.htm"],
    }
    client = StubClient(
        {
            TICKER_MAPPING_URL: {
                "0": {"cik_str": company.cik, "ticker": "AAPL", "title": company.name}
            },
            submissions_url: {
                "filings": {
                    "recent": recent,
                    # Deliberately oldest-first to verify deterministic ordering.
                    "files": [
                        {
                            "name": older_name,
                            "filingFrom": "2024-01-01",
                            "filingTo": "2024-12-31",
                        },
                        {
                            "name": newer_name,
                            "filingFrom": "2025-01-01",
                            "filingTo": "2025-12-31",
                        },
                    ],
                }
            },
            newer_url: newer_history,
            older_url: older_history,
        }
    )

    _, filings = discover_filings(client, "AAPL", "10-K", 3)

    assert [filing.accession_number for filing in filings] == [
        "0000320193-26-000001",
        "0000320193-25-000002",
        "0000320193-24-000004",
    ]
    assert all(filing.form == "10-K" for filing in filings)
    assert client.requested == [
        TICKER_MAPPING_URL,
        submissions_url,
        newer_url,
        older_url,
    ]
