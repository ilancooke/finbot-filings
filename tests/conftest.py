from __future__ import annotations

from datetime import date

import pytest

from finbot_filings.models import Company, Filing
from finbot_filings.sec.filings import filing_index_url, primary_document_url


@pytest.fixture
def company() -> Company:
    return Company(ticker="AAPL", name="Apple Inc.", cik=320193)


@pytest.fixture
def filing(company: Company) -> Filing:
    accession = "0000320193-25-000079"
    return Filing(
        ticker=company.ticker,
        company_name=company.name,
        cik=company.cik,
        form="10-K",
        accession_number=accession,
        filing_date=date(2025, 10, 31),
        report_date=date(2025, 9, 27),
        primary_document="aapl-20250927.htm",
        filing_url=filing_index_url(company.cik, accession),
        document_url=primary_document_url(
            company.cik, accession, "aapl-20250927.htm"
        ),
    )

