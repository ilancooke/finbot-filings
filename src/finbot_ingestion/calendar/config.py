"""Isolated calendar settings; no credentials, clients or implicit .env loading."""

from dataclasses import dataclass
import math
import os

from finbot_ingestion.config import ConfigurationError
from finbot_ingestion.ingestion.retry_policy import RetryPolicy
from .contracts import provider_name


@dataclass(frozen=True, slots=True)
class CalendarConfig:
    provider: str = "placeholder"
    lookahead_days: int = 30
    near_term_days: int = 3
    full_refresh_seconds: float = 86400
    near_term_refresh_seconds: float = 0
    stale_after_seconds: float = 172800
    max_companies: int = 1000
    max_snapshot_events: int = 5000
    company_page_size: int = 100
    provider_attempts: int = 3
    checkpoint_attempts: int = 3
    backoff_base_seconds: float = 1
    backoff_cap_seconds: float = 30

    def __post_init__(self):
        try:
            provider_name(self.provider)
            for name in ("lookahead_days", "near_term_days", "max_companies", "max_snapshot_events",
                         "company_page_size", "provider_attempts", "checkpoint_attempts"):
                if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                    raise ValueError(f"{name} must be a positive integer")
            if self.company_page_size > 1000 or self.near_term_days > self.lookahead_days:
                raise ValueError("invalid page size or near-term span")
            for name in ("full_refresh_seconds", "near_term_refresh_seconds", "stale_after_seconds"):
                value = getattr(self, name)
                if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                        or value < 0 or (name != "near_term_refresh_seconds" and value == 0)):
                    raise ValueError(f"{name} must be finite and positive (near-term zero disables it)")
            RetryPolicy(self.provider_attempts, self.backoff_base_seconds, self.backoff_cap_seconds)
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc

    @classmethod
    def from_env(cls, environ=None):
        values = os.environ if environ is None else environ
        try:
            options = {}
            for name in cls.__dataclass_fields__:
                key = "CALENDAR_" + name.upper()
                if key in values:
                    convert = str if name == "provider" else float if name.endswith("seconds") else int
                    options[name] = convert(values[key])
            if options.get("provider") == "yahoo" and "provider_attempts" not in options:
                options["provider_attempts"] = 1
            return cls(**options)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("invalid calendar configuration") from exc
