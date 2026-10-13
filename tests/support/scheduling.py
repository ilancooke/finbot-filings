"""Reusable offline scheduling test support."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from finbot_ingestion.domain import Company, ExpectedEarningsEvent, Filing
from finbot_ingestion.domain.sec_items import FilingObservation, SECItemMetadata


NOW = datetime(2026, 10, 8, 20, tzinfo=timezone.utc)
COMPANY = Company("AAPL", "320193", "Apple")
ACCESSION = "0000320193-26-000001"


class FakeClock:
    def __init__(self):
        self.elapsed = 0.0
        self.wall = NOW

    def monotonic(self):
        return self.elapsed

    def now(self):
        return self.wall + timedelta(seconds=self.elapsed)

    def advance(self, seconds):
        self.elapsed += seconds


def event(**changes):
    return replace(ExpectedEarningsEvent(company_cik=COMPANY.cik, ticker="AAPL", provider="fixture",
        expected_date=NOW.date(), time_of_day="after_market", synced_at=NOW, provider_event_id="quarter"), **changes)


def observation(form="8-K", status="known", items=("2.02",), **changes):
    filing = replace(Filing(accession_number=ACCESSION, company_cik=COMPANY.cik, ticker="AAPL",
        form_type=form, filed_at=NOW, discovered_at=NOW, filing_index_url="https://www.sec.gov/example"), **changes)
    return FilingObservation(filing, SECItemMetadata(filing.accession_number, filing.company_cik, status, items))
