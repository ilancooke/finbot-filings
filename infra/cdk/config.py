"""Explicit inputs; no AWS lookups or credential resolution during synthesis."""
from dataclasses import dataclass
import json
import math
import re
from pathlib import Path


@dataclass(frozen=True)
class InfraConfig:
    account: str
    region: str
    environment: str
    prefix: str
    sec_user_agent: str
    github_subject: str
    image_digest: str = "sha256:" + "0" * 64
    github_oidc_provider_arn: str | None = None
    alarm_action_arn: str | None = None
    alarm_email: str | None = None
    monitoring_enabled: bool = False
    cpu: int = 512
    memory_mib: int = 1024
    sec_error_threshold: int = 10
    publish_error_threshold: int = 3
    discovery_latency_ms: float = 60000
    failed_work_age_seconds: float = 300
    calendar_provider: str = "placeholder"
    calendar_lookahead_days: int = 30
    calendar_full_refresh_seconds: float = 86400
    calendar_near_term_refresh_seconds: float = 0
    yahoo_cache_dir: str = "/tmp/finbot-yahoo"

    def __post_init__(self):
        if self.calendar_provider not in {"placeholder", "yahoo"}:
            raise ValueError("calendar provider must be placeholder or yahoo")
        if type(self.calendar_lookahead_days) is not int or not 3 <= self.calendar_lookahead_days <= 360:
            raise ValueError("calendar lookahead must be 3–360 days including repository lookback margin")
        for name in ("calendar_full_refresh_seconds", "calendar_near_term_refresh_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (name == "calendar_full_refresh_seconds" and value == 0):
                raise ValueError("invalid calendar refresh interval")
        path = Path(self.yahoo_cache_dir)
        if not path.is_absolute() or path == Path("/tmp") or Path("/tmp") not in path.parents or ".." in path.parts:
            raise ValueError("ECS Yahoo cache must be a private directory below writable /tmp")
        if not re.fullmatch(r"\d{12}", self.account):
            raise ValueError("account must contain 12 digits")
        if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", self.region):
            raise ValueError("invalid region")
        for name in (self.environment, self.prefix):
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", name):
                raise ValueError("environment/prefix must be lowercase resource names")
        if not self.sec_user_agent.strip() or "@" not in self.sec_user_agent or len(self.sec_user_agent) > 256:
            raise ValueError("SEC identity must include a contact address")
        # Exact subject only: never allow a wildcard repository, branch or environment.
        if not re.fullmatch(r"repo:[^/*:]+/[^/*:]+:(?:ref:refs/heads/main|environment:[^*]+)", self.github_subject):
            raise ValueError("require exact GitHub main branch or protected environment subject")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_digest):
            raise ValueError("image must be a resolved SHA256 digest")
        if type(self.monitoring_enabled) is not bool:
            raise ValueError("monitoring_enabled must be boolean")
        if self.alarm_email is not None and (not isinstance(self.alarm_email, str) or
                not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", self.alarm_email)):
            raise ValueError("alarm_email must be an email address")
        if self.alarm_email and self.alarm_action_arn:
            raise ValueError("choose alarm_email or an existing alarm_action_arn")
        if self.monitoring_enabled and not (self.alarm_email or self.alarm_action_arn):
            raise ValueError("monitoring requires a notification destination")
        for value in (self.sec_error_threshold, self.publish_error_threshold, self.discovery_latency_ms, self.failed_work_age_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("alarm thresholds must be finite and positive")
        if self.cpu not in (256, 512, 1024) or self.memory_mib not in {
            256: (512, 1024, 2048), 512: (1024, 2048, 3072, 4096),
            1024: tuple(range(2048, 8193, 1024))}[self.cpu]:
            raise ValueError("unsupported v0 Fargate CPU/memory combination")
        if self.github_oidc_provider_arn and self.github_oidc_provider_arn != (
            f"arn:aws:iam::{self.account}:oidc-provider/token.actions.githubusercontent.com"):
            raise ValueError("OIDC provider must be this account's GitHub provider")
        if self.alarm_action_arn and not self.alarm_action_arn.startswith(
            f"arn:aws:sns:{self.region}:{self.account}:"):
            raise ValueError("alarm action must be an existing same-account/region SNS topic")

    @classmethod
    def read(cls, path):
        return cls(**json.loads(Path(path).read_text()))

    @property
    def name(self):
        return f"{self.prefix}-{self.environment}"
