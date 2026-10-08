"""Workflow settings; independent of SEC, SDK and legacy configuration."""

from dataclasses import dataclass
import os

from finbot_ingestion.config import ConfigurationError
from .retry_policy import RetryPolicy


@dataclass(frozen=True, slots=True)
class WorkflowConfig:
    max_stage_failures: int = 3
    checkpoint_attempts: int = 3
    dead_letter_attempts: int = 3
    max_inflight_artifacts: int = 2
    recovery_page_size: int = 100
    backoff_base_seconds: float = 1
    backoff_cap_seconds: float = 30

    def __post_init__(self):
        for name in ("max_stage_failures", "checkpoint_attempts", "dead_letter_attempts",
                     "max_inflight_artifacts", "recovery_page_size"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ConfigurationError(f"{name} must be a positive integer")
        if self.recovery_page_size > 1000:
            raise ConfigurationError("recovery_page_size must be at most 1000")
        try:
            RetryPolicy(self.max_stage_failures, self.backoff_base_seconds, self.backoff_cap_seconds)
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc

    @classmethod
    def from_env(cls, environ=None):
        values = os.environ if environ is None else environ
        try:
            options = {name: convert(values["INGESTION_" + name.upper()])
                       for name, convert in (("max_stage_failures", int), ("checkpoint_attempts", int),
                          ("dead_letter_attempts", int), ("max_inflight_artifacts", int),
                          ("recovery_page_size", int), ("backoff_base_seconds", float),
                          ("backoff_cap_seconds", float))
                       if "INGESTION_" + name.upper() in values}
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("invalid workflow configuration") from exc
        return cls(**options)
