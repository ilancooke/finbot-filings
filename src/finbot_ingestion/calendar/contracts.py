"""Provider-independent calendar snapshots and durable synchronization facts."""

from dataclasses import dataclass
from datetime import date, datetime
import re

from finbot_ingestion.domain import ExpectedEarningsEvent
from finbot_ingestion.domain.identity import normalize_cik
from finbot_ingestion.domain.validation import utc_datetime


def provider_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise ValueError("provider must be a lowercase identifier of at most 64 characters")
    return value


def date_range(start, end):
    if type(start) is not date or type(end) is not date or start > end:
        raise ValueError("an ordered inclusive date range is required")


def company_scope(values):
    result = tuple(sorted(normalize_cik(value) for value in values))
    if len(set(result)) != len(result):
        raise ValueError("company scope contains duplicate CIKs")
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class CalendarSnapshot:
    provider: str
    start_date: date
    end_date: date
    company_ciks: tuple[str, ...]
    events: tuple[ExpectedEarningsEvent, ...]
    complete: bool

    def __post_init__(self):
        provider_name(self.provider)
        date_range(self.start_date, self.end_date)
        object.__setattr__(self, "company_ciks", company_scope(self.company_ciks))
        object.__setattr__(self, "events", tuple(self.events))
        if type(self.complete) is not bool:
            raise ValueError("complete must be boolean")
        if any(not isinstance(event, ExpectedEarningsEvent) for event in self.events):
            raise ValueError("snapshot events must be normalized expectations")


@dataclass(frozen=True, slots=True, kw_only=True)
class CalendarSyncRun:
    request_id: str
    provider: str
    kind: str
    start_date: date
    end_date: date
    company_ciks: tuple[str, ...]
    observed_at: datetime

    def __post_init__(self):
        provider_name(self.provider)
        if self.kind not in {"full", "near_term"}:
            raise ValueError("sync kind must be full or near_term")
        if not isinstance(self.request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.request_id):
            raise ValueError("request_id must be a bounded opaque identifier")
        date_range(self.start_date, self.end_date)
        object.__setattr__(self, "company_ciks", company_scope(self.company_ciks))
        object.__setattr__(self, "observed_at", utc_datetime(self.observed_at, "observed_at"))


@dataclass(frozen=True, slots=True, kw_only=True)
class CalendarSyncSuccess:
    run: CalendarSyncRun
    completed_at: datetime
    event_count: int
    cancelled_count: int

    def __post_init__(self):
        object.__setattr__(self, "completed_at", utc_datetime(self.completed_at, "completed_at"))
        if self.completed_at < self.run.observed_at:
            raise ValueError("completion precedes observation")
        for value in (self.event_count, self.cancelled_count):
            if type(value) is not int or value < 0:
                raise ValueError("sync counts must be nonnegative integers")


@dataclass(frozen=True, slots=True, kw_only=True)
class CalendarSyncState:
    provider: str
    revision: int
    latest_run: CalendarSyncRun
    full_success: CalendarSyncSuccess | None = None
    near_term_success: CalendarSyncSuccess | None = None
    failure_run: CalendarSyncRun | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None

    def __post_init__(self):
        provider_name(self.provider)
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("revision must be a nonnegative integer")
        if self.latest_run.provider != self.provider:
            raise ValueError("state provider disagrees with run")
        for kind in ("full", "near_term"):
            success = getattr(self, kind + "_success")
            if success is not None and (success.run.provider != self.provider or success.run.kind != kind
                    or success.run.observed_at > self.latest_run.observed_at):
                raise ValueError("invalid successful sync checkpoint")
        fields = (self.failure_run, self.last_error, self.last_error_at)
        if any(value is not None for value in fields):
            if any(value is None for value in fields):
                raise ValueError("failure facts must be recorded together")
            if not isinstance(self.last_error, str) or not 1 <= len(self.last_error) <= 2048:
                raise ValueError("invalid sync error")
            object.__setattr__(self, "last_error_at", utc_datetime(self.last_error_at, "last_error_at"))
            if (self.failure_run.provider != self.provider
                    or self.failure_run.observed_at > self.latest_run.observed_at
                    or self.last_error_at < self.failure_run.observed_at):
                raise ValueError("invalid sync failure checkpoint")


@dataclass(frozen=True, slots=True)
class CalendarHealth:
    state: CalendarSyncState | None
    full_sync_age_seconds: float | None
    stale: bool
