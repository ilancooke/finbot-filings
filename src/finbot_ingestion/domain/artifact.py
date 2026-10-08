"""Raw child-document identity and durable processing facts."""

from dataclasses import dataclass, field
from datetime import datetime

from .identity import (
    artifact_identity,
    normalize_accession_number,
    normalize_cik,
    normalize_form,
    normalize_ticker,
)
from .validation import utc_datetime, validate_s3_uri


@dataclass(frozen=True, slots=True, kw_only=True)
class Artifact:
    accession_number: str
    company_cik: str
    ticker: str
    form_type: str
    filename: str
    sec_url: str
    discovered_at: datetime
    artifact_id: str = field(init=False)
    document_type: str | None = None
    s3_uri: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    stored_at: datetime | None = None
    published_at: datetime | None = None
    retry_count: int = 0
    last_error: str | None = None
    last_error_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "accession_number", normalize_accession_number(self.accession_number))
        object.__setattr__(self, "company_cik", normalize_cik(self.company_cik))
        object.__setattr__(self, "ticker", normalize_ticker(self.ticker))
        object.__setattr__(self, "form_type", normalize_form(self.form_type))
        object.__setattr__(self, "artifact_id", artifact_identity(self.accession_number, self.filename))
        object.__setattr__(self, "discovered_at", utc_datetime(self.discovered_at, "discovered_at"))
        for name in ("stored_at", "published_at", "last_error_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, utc_datetime(value, name))
        if type(self.retry_count) is not int or self.retry_count < 0:
            raise ValueError("retry_count must be a nonnegative integer")
        if self.size_bytes is not None and (
            type(self.size_bytes) is not int or self.size_bytes < 0
        ):
            raise ValueError("size_bytes must be a nonnegative integer")
        if (self.s3_uri is None) != (self.stored_at is None):
            raise ValueError("s3_uri and stored_at must be recorded together")
        if self.s3_uri is not None:
            validate_s3_uri(self.s3_uri)
        if self.published_at is not None and self.stored_at is None:
            raise ValueError("publication requires a stored artifact")
