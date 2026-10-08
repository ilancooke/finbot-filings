"""Isolated storage/messaging SDK configuration; no client on import."""

from dataclasses import dataclass
import math
import os
import re
from urllib.parse import urlsplit

from botocore.config import Config

from .config import ConfigurationError


@dataclass(frozen=True, slots=True)
class AWSIOConfig:
    region: str
    connect_timeout_seconds: float = 5
    read_timeout_seconds: float = 30
    max_attempts: int = 3
    max_workers: int = 2

    def __post_init__(self):
        if not isinstance(self.region, str) or not re.fullmatch(r"[a-z0-9-]+", self.region):
            raise ConfigurationError("AWS_REGION is required")
        for name in ("connect_timeout_seconds", "read_timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"{name} must be finite and positive")
        for name in ("max_attempts", "max_workers"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ConfigurationError(f"{name} must be a positive integer")

    def sdk_config(self):
        return Config(connect_timeout=self.connect_timeout_seconds, read_timeout=self.read_timeout_seconds,
                      retries={"mode": "standard", "total_max_attempts": self.max_attempts},
                      max_pool_connections=self.max_workers)

    @classmethod
    def from_env(cls, environ=None):
        values = os.environ if environ is None else environ
        try:
            options = {name: convert(values["INGESTION_AWS_" + name.upper()])
                       for name, convert in (("connect_timeout_seconds", float), ("read_timeout_seconds", float),
                                             ("max_attempts", int), ("max_workers", int))
                       if "INGESTION_AWS_" + name.upper() in values}
        except (ValueError, TypeError) as exc:
            raise ConfigurationError("invalid ingestion AWS I/O configuration") from exc
        return cls(region=values.get("AWS_REGION", ""), **options)


@dataclass(frozen=True, slots=True)
class StorageConfig:
    bucket: str
    max_artifact_bytes: int = 64 * 1024 * 1024

    def __post_init__(self):
        if (not isinstance(self.bucket, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket)
                or ".." in self.bucket or re.fullmatch(r"\d+\.\d+\.\d+\.\d+", self.bucket)):
            raise ConfigurationError("ARTIFACT_BUCKET must be a general-purpose bucket name")
        if type(self.max_artifact_bytes) is not int or not 1 <= self.max_artifact_bytes <= 64 * 1024 * 1024:
            raise ConfigurationError("MAX_ARTIFACT_BYTES must be in [1, 67108864]")

    @classmethod
    def from_env(cls, environ=None):
        values = os.environ if environ is None else environ
        try:
            limit = int(values.get("MAX_ARTIFACT_BYTES", str(64 * 1024 * 1024)))
        except (ValueError, TypeError) as exc:
            raise ConfigurationError("invalid MAX_ARTIFACT_BYTES") from exc
        return cls(values.get("ARTIFACT_BUCKET", ""), limit)


@dataclass(frozen=True, slots=True)
class MessagingConfig:
    region: str
    topic_arn: str
    dead_letter_queue_url: str

    def __post_init__(self):
        AWSIOConfig(self.region)
        match = re.fullmatch(r"arn:(aws|aws-us-gov|aws-cn):sns:([a-z0-9-]+):[0-9]{12}:([A-Za-z0-9_-]{1,256})", self.topic_arn)
        if match is None or match[2] != self.region:
            raise ConfigurationError("ARTIFACT_READY_TOPIC_ARN must be a same-region standard SNS topic")
        parts = urlsplit(self.dead_letter_queue_url)
        suffix = "amazonaws.com.cn" if match[1] == "aws-cn" else "amazonaws.com"
        if (parts.scheme != "https" or parts.netloc != f"sqs.{self.region}.{suffix}"
                or parts.query or parts.fragment or not re.fullmatch(r"/[0-9]{12}/[A-Za-z0-9_-]{1,80}", parts.path)):
            raise ConfigurationError("INGESTION_DEAD_LETTER_QUEUE_URL must be a same-region standard SQS queue")

    @classmethod
    def from_env(cls, environ=None):
        values = os.environ if environ is None else environ
        return cls(values.get("AWS_REGION", ""), values.get("ARTIFACT_READY_TOPIC_ARN", ""),
                   values.get("INGESTION_DEAD_LETTER_QUEUE_URL", ""))
