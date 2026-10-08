"""Ingestion configuration; no implicit .env or legacy folder requirements."""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigurationError(ValueError):
    """Invalid ingestion configuration."""


@dataclass(frozen=True, slots=True)
class IngestionConfig:
    sec_user_agent: str
    sec_max_requests_per_second: float = 5.0

    sec_connect_timeout_seconds: float = 10.0
    sec_read_timeout_seconds: float = 30.0
    sec_max_attempts: int = 3
    sec_backoff_base_seconds: float = 1.0
    sec_backoff_cap_seconds: float = 30.0
    sec_max_redirects: int = 5

    def __post_init__(self) -> None:
        if (not isinstance(self.sec_user_agent, str) or not self.sec_user_agent.strip()
                or any(ord(c) < 32 or ord(c) == 127 for c in self.sec_user_agent)):
            raise ConfigurationError("SEC_USER_AGENT must be a nonempty identifying header")
        object.__setattr__(self, "sec_user_agent", self.sec_user_agent.strip())
        for name in ("sec_connect_timeout_seconds", "sec_read_timeout_seconds", "sec_backoff_base_seconds", "sec_backoff_cap_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"{name} must be finite and positive")
        for name, minimum in (("sec_max_attempts", 1), ("sec_max_redirects", 0)):
            if type(getattr(self, name)) is not int or getattr(self, name) < minimum:
                raise ConfigurationError(f"{name} must be an integer >= {minimum}")
        if self.sec_backoff_cap_seconds < self.sec_backoff_base_seconds:
            raise ConfigurationError("backoff cap must be >= base")
        rate = self.sec_max_requests_per_second
        if (isinstance(rate, bool) or not isinstance(rate, (int, float))
                or not math.isfinite(rate) or not 0 < rate <= 5):
            raise ConfigurationError("SEC_MAX_REQUESTS_PER_SECOND must be finite and > 0, <= 5")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "IngestionConfig":
        """Read process environment (or an injected mapping), without reading .env."""
        values = os.environ if environ is None else environ
        try:
            rate = float(values.get("SEC_MAX_REQUESTS_PER_SECOND", "5"))
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("SEC_MAX_REQUESTS_PER_SECOND must be numeric") from exc
        options = {}
        try:
            for name in ("sec_connect_timeout_seconds", "sec_read_timeout_seconds", "sec_max_attempts", "sec_backoff_base_seconds", "sec_backoff_cap_seconds", "sec_max_redirects"):
                if name.upper() in values:
                    convert = int if name in ("sec_max_attempts", "sec_max_redirects") else float
                    options[name] = convert(values[name.upper()])
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("invalid SEC transport configuration") from exc
        return cls(values.get("SEC_USER_AGENT", ""), rate, **options)
