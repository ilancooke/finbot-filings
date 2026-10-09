"""Durable versioned polling satisfaction, separate from mutable expectations."""

from dataclasses import dataclass
from datetime import date, datetime
import json

from finbot_ingestion.calendar.contracts import provider_name
from .identity import normalize_cik, normalize_accession_number
from .validation import utc_datetime
from .sec_items import SECItemMetadata
from finbot_ingestion.scheduler.earnings_satisfaction_policy import POLICY_VERSION, MATCH_REASONS


@dataclass(frozen=True, slots=True)
class EventIdentity:
    provider: str
    company_cik: str
    kind: str
    value: str

    def __post_init__(self):
        provider_name(self.provider)
        object.__setattr__(self, "company_cik", normalize_cik(self.company_cik))
        if self.kind not in ("id", "date") or not isinstance(self.value, str) or not self.value:
            raise ValueError("invalid event identity")
        if self.kind == "date" and date.fromisoformat(self.value).isoformat() != self.value:
            raise ValueError("invalid identity date")
        if len(self.key.encode("utf-8")) > 1024:
            raise ValueError("event identity exceeds DynamoDB sort-key limit")

    @property
    def key(self):
        return json.dumps([self.provider, self.company_cik, self.kind, self.value], ensure_ascii=True, separators=(",", ":"))

    @classmethod
    def from_event(cls, event):
        return cls(event.provider, event.company_cik, "id" if event.provider_event_id is not None else "date",
                   event.provider_event_id if event.provider_event_id is not None else event.expected_date.isoformat())


@dataclass(frozen=True, slots=True)
class EventSatisfaction:
    identity: EventIdentity
    matched_accession_number: str
    matched_filed_at: datetime
    satisfied_at: datetime
    observed_expected_date: date
    window_start: datetime
    window_end: datetime
    grace_end: datetime
    matched_form_type: str
    match_reason: str
    match_policy_version: str = POLICY_VERSION
    matched_sec_items: tuple[str, ...] = ()
    sec_item_evidence_source: str | None = None

    def __post_init__(self):
        if not isinstance(self.identity, EventIdentity) or type(self.observed_expected_date) is not date:
            raise ValueError("typed event identity/date required")
        object.__setattr__(self, "matched_accession_number", normalize_accession_number(self.matched_accession_number))
        for name in ("matched_filed_at", "satisfied_at", "window_start", "window_end", "grace_end"):
            object.__setattr__(self, name, utc_datetime(getattr(self, name), name))
        if not (self.window_start < self.window_end <= self.grace_end and
                self.window_start <= self.matched_filed_at < self.grace_end and self.satisfied_at >= self.matched_filed_at):
            raise ValueError("satisfaction outside valid acceptance window")
        if self.identity.kind == "date" and self.identity.value != self.observed_expected_date.isoformat():
            raise ValueError("identity date differs from expectation")
        if (self.match_policy_version != POLICY_VERSION or self.matched_form_type not in MATCH_REASONS
                or MATCH_REASONS[self.matched_form_type] != self.match_reason):
            raise ValueError("unsupported policy version or match reason")
        evidence = SECItemMetadata(self.matched_accession_number, self.identity.company_cik,
            "known" if self.sec_item_evidence_source is not None else "absent", self.matched_sec_items,
            self.sec_item_evidence_source or "submissions.recent.items")
        if self.matched_form_type == "8-K" and (evidence.status != "known" or "2.02" not in evidence.items):
            raise ValueError("8-K satisfaction requires SEC Item 2.02 evidence")

    @classmethod
    def from_match(cls, event, window, observation, decision, *, at):
        if not decision.eligible:
            raise ValueError("positive policy decision required")
        f, e = observation.filing, observation.sec_items
        if f.company_cik != event.company_cik:
            raise ValueError("matched filing CIK differs")
        return cls(EventIdentity.from_event(event), f.accession_number, f.filed_at, at,
            event.expected_date, window.start, window.end, window.grace_end, f.form_type,
            decision.reason, decision.policy_version, e.items if e.status == "known" else (),
            e.source if e.status == "known" else None)
