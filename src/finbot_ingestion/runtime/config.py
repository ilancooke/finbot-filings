"""Validated environment-only runtime settings."""

from dataclasses import dataclass, fields
import math
import os
from zoneinfo import ZoneInfo

from finbot_ingestion.config import ConfigurationError


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    market_timezone: str = "America/New_York"
    market_calendar: str = "XNYS"
    active_poll_seconds: float = 10
    safety_poll_seconds: float = 3600
    before_open_seconds: float = 7200
    after_open_seconds: float = 7200
    before_close_seconds: float = 7200
    after_close_seconds: float = 10800
    grace_seconds: float = 7200
    non_session_start_hour: int = 7
    non_session_start_minute: int = 30
    non_session_end_hour: int = 19
    reload_seconds: float = 300
    recovery_seconds: float = 60
    tick_seconds: float = 1
    refresh_retry_seconds: float = 60
    poll_workers: int = 2
    enumeration_workers: int = 1
    company_queue_size: int = 1000
    filing_queue_size: int = 100
    artifact_queue_size: int = 200
    metrics_flush_seconds: float = 5
    heartbeat_seconds: float = 30
    stall_seconds: float = 1800
    shutdown_grace_seconds: float = 30
    health_path: str = "/tmp/finbot-ingestion-health.json"

    def __post_init__(self):
        try:
            ZoneInfo(self.market_timezone)
            if self.market_calendar != "XNYS":
                raise ValueError("v0 supports XNYS market sessions")
            for field in fields(self):
                value = getattr(self, field.name)
                if field.name.endswith("seconds"):
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                        raise ValueError(f"{field.name} must be finite and positive")
                elif field.name.endswith(("workers", "size")):
                    bound = 16 if field.name.endswith("workers") else 10000
                    if type(value) is not int or not 1 <= value <= bound:
                        raise ValueError(f"{field.name} must be an integer in [1, {bound}]")
            for name in ("non_session_start_hour", "non_session_end_hour"):
                if type(getattr(self, name)) is not int or not 0 <= getattr(self, name) <= 23:
                    raise ValueError("invalid fallback hour")
            if type(self.non_session_start_minute) is not int or not 0 <= self.non_session_start_minute <= 59:
                raise ValueError("invalid fallback minute")
            if self.non_session_start_hour * 60 + self.non_session_start_minute >= self.non_session_end_hour * 60:
                raise ValueError("fallback window must be ordered")
            if self.active_poll_seconds > self.safety_poll_seconds:
                raise ValueError("active interval must not exceed safety interval")
            if not isinstance(self.health_path, str) or not self.health_path.startswith("/"):
                raise ValueError("health_path must be absolute")
        except (ValueError, KeyError) as exc:
            raise ConfigurationError(str(exc)) from exc

    @classmethod
    def from_env(cls, environ=None):
        values = os.environ if environ is None else environ
        try:
            options = {}
            for field in fields(cls):
                key = "RUNTIME_" + field.name.upper()
                if key in values:
                    convert = float if field.name.endswith("seconds") else int if field.type is int else str
                    options[field.name] = convert(values[key])
            return cls(**options)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("invalid runtime configuration") from exc
