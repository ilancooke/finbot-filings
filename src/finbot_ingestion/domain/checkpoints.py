"""Durable processing facts kept separate from immutable SEC submissions."""

from dataclasses import dataclass
from datetime import datetime

from .identity import normalize_accession_number, validate_filename
from .validation import utc_datetime


STAGES = frozenset({"ENUMERATE", "ACQUIRE", "PUBLISH"})
STAGE_COUNTERS = {"ENUMERATE": "enumeration_failures", "ACQUIRE": "acquisition_failures",
                  "PUBLISH": "publication_failures"}


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessingCheckpoint:
    """Durable workflow failures; legacy retry_count is a separate diagnostic."""

    enumeration_failures: int = 0
    acquisition_failures: int = 0
    publication_failures: int = 0
    failure_stage: str | None = None
    failure_error: str | None = None
    failure_at: datetime | None = None
    terminal_stage: str | None = None
    terminal_error: str | None = None
    terminal_error_type: str | None = None
    terminal_at: datetime | None = None
    dead_lettered_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in STAGE_COUNTERS.values():
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        for name in ("failure_at", "terminal_at", "dead_lettered_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, utc_datetime(value, name))
        for names in (("failure_stage", "failure_error", "failure_at"),
                      ("terminal_stage", "terminal_error", "terminal_error_type", "terminal_at")):
            values = [getattr(self, name) for name in names]
            if any(value is not None for value in values):
                if any(value is None for value in values) or values[0] not in STAGES:
                    raise ValueError("failure fields must be paired with a valid stage")
                if any(not isinstance(value, str) or not value or len(value) > 2048
                       for value in values[1:-1]):
                    raise ValueError("failure text must be nonempty and bounded")
                if getattr(self, STAGE_COUNTERS[values[0]]) < 1:
                    raise ValueError("failure requires a stage counter")
        if any(getattr(self, name) for name in STAGE_COUNTERS.values()) and self.failure_stage is None:
            raise ValueError("stage counters require a failure observation")
        if self.terminal_at is not None and (
                self.terminal_stage != self.failure_stage or self.terminal_at != self.failure_at
                or self.terminal_error != self.failure_error):
            raise ValueError("terminal facts must describe the final stage failure")
        if self.dead_lettered_at is not None and self.terminal_at is None:
            raise ValueError("dead-letter checkpoint requires terminal facts")

    def failures(self, stage: str) -> int:
        return getattr(self, STAGE_COUNTERS[stage])

    @property
    def pending_dead_letter(self) -> bool:
        return self.terminal_at is not None and self.dead_lettered_at is None


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactCheckpoint(ProcessingCheckpoint):
    artifact_id: str

    def __post_init__(self) -> None:
        ProcessingCheckpoint.__post_init__(self)
        from .identity import artifact_identity
        if not isinstance(self.artifact_id, str) or self.artifact_id.count("/") != 1:
            raise ValueError("invalid artifact identity")
        accession, filename = self.artifact_id.split("/")
        if artifact_identity(accession, filename) != self.artifact_id:
            raise ValueError("invalid artifact identity")
        if self.enumeration_failures or self.failure_stage == "ENUMERATE":
            raise ValueError("artifacts cannot have enumeration failures")


@dataclass(frozen=True, slots=True, kw_only=True)
class FilingCheckpoint(ProcessingCheckpoint):
    accession_number: str
    enumeration_completed_at: datetime | None = None
    resolved_primary_document_name: str | None = None
    enumerated_artifact_count: int | None = None
    retry_count: int = 0
    last_error: str | None = None
    last_error_at: datetime | None = None

    def __post_init__(self) -> None:
        ProcessingCheckpoint.__post_init__(self)
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
        if self.acquisition_failures or self.publication_failures or self.failure_stage not in (None, "ENUMERATE"):
            raise ValueError("filings can only have enumeration failures")
        if self.terminal_at is not None and self.enumeration_completed_at is not None:
            raise ValueError("completed enumeration cannot be terminal")
