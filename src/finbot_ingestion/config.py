"""Phase 1 configuration; no network access or legacy folder requirements."""

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

    def __post_init__(self) -> None:
        if (not isinstance(self.sec_user_agent, str) or not self.sec_user_agent.strip()
                or any(ord(c) < 32 or ord(c) == 127 for c in self.sec_user_agent)):
            raise ConfigurationError("SEC_USER_AGENT must be a nonempty identifying header")
        object.__setattr__(self, "sec_user_agent", self.sec_user_agent.strip())
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
        return cls(values.get("SEC_USER_AGENT", ""), rate)
