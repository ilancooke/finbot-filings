"""Provider-independent expectations, not regulatory publication facts."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
import re

from .identity import normalize_cik, normalize_ticker
from .validation import utc_datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ExpectedEarningsEvent:
    company_cik: str
    ticker: str
    expected_date: date
    synced_at: datetime
    provider: str
    time_of_day: str | None = None
    provider_event_id: str | None = None
    provider_updated_at: datetime | None = None
    raw_provider_payload: dict[str, Any] | None = None
    replacement_hint: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "company_cik", normalize_cik(self.company_cik))
        object.__setattr__(self, "ticker", normalize_ticker(self.ticker))
        if type(self.expected_date) is not date:
            raise ValueError("expected_date must be a date")
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("provider must be nonempty")
        object.__setattr__(self, "synced_at", utc_datetime(self.synced_at, "synced_at"))
        if self.provider_updated_at is not None:
            object.__setattr__(self, "provider_updated_at", utc_datetime(
                self.provider_updated_at, "provider_updated_at"))
        if self.replacement_hint is not None and (not isinstance(self.replacement_hint, str)
                or not re.fullmatch(r"quarterly-announcement-v1/[12]\d{3}/Q[1-4]", self.replacement_hint)):
            raise ValueError("invalid versioned calendar replacement hint")
