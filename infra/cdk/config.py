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
    monitoring_enabled: bool = False
    cpu: int = 512
    memory_mib: int = 1024
    sec_error_threshold: int = 10
    publish_error_threshold: int = 3
    discovery_latency_ms: float = 60000
    failed_work_age_seconds: float = 300

    def __post_init__(self):
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
