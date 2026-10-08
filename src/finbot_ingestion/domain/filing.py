"""One immutable submission, identified by SEC accession number."""

from dataclasses import dataclass
from datetime import datetime

from .identity import (
    normalize_accession_number, normalize_cik, normalize_form,
    normalize_ticker, validate_filename,
)
from .validation import utc_datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class Filing:
    accession_number: str
    company_cik: str
    ticker: str
    form_type: str
    filed_at: datetime
    discovered_at: datetime
    filing_index_url: str
    primary_document_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "accession_number", normalize_accession_number(self.accession_number))
        object.__setattr__(self, "company_cik", normalize_cik(self.company_cik))
        object.__setattr__(self, "ticker", normalize_ticker(self.ticker))
        object.__setattr__(self, "form_type", normalize_form(self.form_type))
        for name in ("filed_at", "discovered_at"):
            object.__setattr__(self, name, utc_datetime(getattr(self, name), name))
        if self.primary_document_name is not None:
            validate_filename(self.primary_document_name)
