"""Yahoo-only bounds, without clients, files or credentials on configuration reads."""

from dataclasses import dataclass, fields
import math
import os
from pathlib import Path

from finbot_ingestion.config import ConfigurationError


@dataclass(frozen=True, slots=True)
class YahooConfig:
    http_timeout_seconds: float = 20
    min_request_interval_seconds: float = 1
    page_size: int = 100
    max_pages_per_slice: int = 20
    max_http_attempts: int = 300
    max_raw_rows: int = 30000
    max_fetch_seconds: float = 600
    cache_dir: str = "/tmp/finbot-yahoo"
    request_attempts: int = 2
    backoff_cap_seconds: float = 30

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name.endswith("seconds"):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    raise ConfigurationError(f"Yahoo {field.name} must be finite and positive")
            elif field.type is int:
                if type(value) is not int or value < 1:
                    raise ConfigurationError(f"Yahoo {field.name} must be a positive integer")
        if self.page_size > 100 or self.request_attempts > 5:
            raise ConfigurationError("Yahoo page size/attempt count exceeds bound")
        if not isinstance(self.cache_dir, str) or not Path(self.cache_dir).is_absolute():
            raise ConfigurationError("Yahoo cache directory must be absolute")
        if self.http_timeout_seconds > self.max_fetch_seconds:
            raise ConfigurationError("Yahoo HTTP timeout exceeds fetch budget")

    @classmethod
    def from_env(cls, environ=None):
        values = os.environ if environ is None else environ
        try:
            options = {}
            for field in fields(cls):
                key = "YAHOO_" + field.name.upper()
                if key in values:
                    convert = str if field.name == "cache_dir" else float if field.name.endswith("seconds") else int
                    options[field.name] = convert(values[key])
            return cls(**options)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("invalid Yahoo configuration") from exc
