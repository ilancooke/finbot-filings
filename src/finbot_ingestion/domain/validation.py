"""Small validation helpers shared by typed contracts."""

from datetime import datetime, timezone
from urllib.parse import urlsplit


def utc_datetime(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def utc_text(value: datetime) -> str:
    return utc_datetime(value, "timestamp").isoformat().replace("+00:00", "Z")


def validate_s3_uri(value: str) -> str:
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise ValueError("s3_uri must be an S3 object URI")
    parsed = urlsplit(value)
    if (parsed.scheme != "s3" or not parsed.netloc or parsed.path in {"", "/"}
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ValueError("s3_uri must be an S3 object URI")
    return value
