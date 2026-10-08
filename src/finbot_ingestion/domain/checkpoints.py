"""Durable processing facts kept separate from immutable SEC submissions."""

from dataclasses import dataclass
from datetime import datetime

from .identity import normalize_accession_number, validate_filename
from .validation import utc_datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class FilingCheckpoint:
    accession_number: str
    enumeration_completed_at: datetime | None = None
    resolved_primary_document_name: str | None = None
    enumerated_artifact_count: int | None = None
    retry_count: int = 0
    last_error: str | None = None
    last_error_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "accession_number", normalize_accession_number(self.accession_number))
        for name in ("enumeration_completed_at", "last_error_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, utc_datetime(value, name))
        completed = (self.enumeration_completed_at, self.resolved_primary_document_name,
                     self.enumerated_artifact_count)
        if any(value is not None for value in completed):
            if any(value is None for value in completed):
                raise ValueError("enumeration checkpoint fields must be recorded together")
            validate_filename(self.resolved_primary_document_name)
            if type(self.enumerated_artifact_count) is not int or self.enumerated_artifact_count < 1:
                raise ValueError("enumerated_artifact_count must be a positive integer")
        if type(self.retry_count) is not int or self.retry_count < 0:
            raise ValueError("retry_count must be a nonnegative integer")
        if (self.last_error is None) != (self.last_error_at is None):
            raise ValueError("error fields must be recorded together")
        if self.last_error is not None and (not isinstance(self.last_error, str) or not self.last_error):
            raise ValueError("last_error must be a nonempty string")
