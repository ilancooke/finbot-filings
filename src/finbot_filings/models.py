"""Typed domain models for companies and SEC filings."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True, slots=True)
class Company:
    ticker: str
    name: str
    cik: int


@dataclass(frozen=True, slots=True)
class Filing:
    ticker: str
    company_name: str
    cik: int
    form: str
    accession_number: str
    filing_date: date
    report_date: date | None
    primary_document: str
    filing_url: str
    document_url: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation of the filing."""
        result = asdict(self)
        result["filing_date"] = self.filing_date.isoformat()
        result["report_date"] = (
            self.report_date.isoformat() if self.report_date is not None else None
        )
        return result

