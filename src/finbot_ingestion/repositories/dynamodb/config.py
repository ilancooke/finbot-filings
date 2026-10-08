"""DynamoDB settings, independent of SEC and later storage/runtime configuration."""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass

from finbot_ingestion.config import ConfigurationError


@dataclass(frozen=True, slots=True)
class DynamoDBConfig:
    region: str
    companies_table: str
    calendar_table: str
    filings_table: str
    artifacts_table: str
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 10.0
    max_attempts: int = 3
    page_size: int = 100
    max_workers: int = 4
    cas_attempts: int = 4
    max_calendar_range_days: int = 366

    def __post_init__(self):
        for name in ("region", "companies_table", "calendar_table", "filings_table", "artifacts_table"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or any(c.isspace() for c in value):
                raise ConfigurationError(f"{name} must be a nonempty identifier without whitespace")
        for name in ("connect_timeout_seconds", "read_timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"{name} must be finite and positive")
        for name in ("max_attempts", "page_size", "max_workers", "cas_attempts", "max_calendar_range_days"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ConfigurationError(f"{name} must be a positive integer")
        if self.page_size > 1000:
            raise ConfigurationError("page_size must be at most 1000")
        if len({self.companies_table, self.calendar_table, self.filings_table, self.artifacts_table}) != 4:
            raise ConfigurationError("four distinct DynamoDB tables are required")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None):
        values = os.environ if environ is None else environ
        options = {}
        try:
            for name in ("connect_timeout_seconds", "read_timeout_seconds", "max_attempts", "page_size", "max_workers", "cas_attempts", "max_calendar_range_days"):
                key = "DYNAMODB_" + name.upper()
                if key in values:
                    options[name] = (float if "timeout" in name else int)(values[key])
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("invalid DynamoDB configuration") from exc
        return cls(region=values.get("AWS_REGION", ""), **{
            name: values.get(name.upper(), "") for name in (
                "companies_table", "calendar_table", "filings_table", "artifacts_table")
        }, **options)
