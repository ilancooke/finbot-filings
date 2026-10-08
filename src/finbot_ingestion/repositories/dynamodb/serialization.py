"""Strict typed records; SDK AttributeValues never escape these adapters."""

import json
from dataclasses import fields
from datetime import date, datetime
from decimal import Decimal

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from finbot_ingestion.domain import Artifact
from finbot_ingestion.domain.validation import utc_datetime
from ..errors import RepositoryDataError

SCHEMA_VERSION = 1
MAX_ITEM_BYTES = 400 * 1024
MAX_ERROR_CHARS = 2048


def timestamp(value: datetime) -> str:
    return utc_datetime(value, "timestamp").isoformat(timespec="microseconds").replace("+00:00", "Z")


def integer(value, name):
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        value = int(value)
    if type(value) is not int or value < 0:
        raise RepositoryDataError(f"{name} must be a nonnegative integer")
    return value


def _size(value):
    # Conservative upper bound using UTF-8 names/values and document overhead.
    if isinstance(value, dict):
        return 3 + sum(len(k.encode("utf-8")) + 1 + _size(v) for k, v in value.items())
    if isinstance(value, list):
        return 3 + sum(1 + _size(v) for v in value)
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if value is None or isinstance(value, bool):
        return 1
    if isinstance(value, (int, Decimal)):
        return len(str(value)) + 1
    raise ValueError("unsupported DynamoDB value")


def encode(item: dict) -> dict:
    try:
        wire = {key: TypeSerializer().serialize(value) for key, value in item.items()}
        if _size(item) > MAX_ITEM_BYTES:
            raise ValueError("DynamoDB item exceeds 400 KB")
        return wire
    except (ValueError, TypeError, ArithmeticError) as exc:
        raise RepositoryDataError(str(exc)) from exc


def decode(item: dict) -> dict:
    try:
        return {key: TypeDeserializer().deserialize(value) for key, value in item.items()}
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        raise RepositoryDataError("invalid DynamoDB AttributeValues") from exc


def _reject_nonfinite_json(value):
    raise ValueError(f"nonfinite JSON value: {value}")


def record(model) -> dict:
    item = {"repository_schema_version": SCHEMA_VERSION}
    for field in fields(model):
        value = getattr(model, field.name)
        if value is None:
            continue
        name = "cik" if field.name == "company_cik" else field.name
        if isinstance(value, datetime):
            value = timestamp(value)
        elif type(value) is date:
            value = value.isoformat()
        elif field.name == "raw_provider_payload":
            try:
                value = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
            except (TypeError, ValueError) as exc:
                raise RepositoryDataError("raw_provider_payload must contain finite JSON values") from exc
            name = "raw_provider_payload_json"
        item[name] = value
    return item


def restore(model, item: dict):
    try:
        if integer(item.get("repository_schema_version"), "schema version") != SCHEMA_VERSION:
            raise ValueError("unsupported repository schema version")
        values = {}
        for field in fields(model):
            if not field.init:
                continue
            name = "cik" if field.name == "company_cik" else field.name
            if field.name == "raw_provider_payload":
                name = "raw_provider_payload_json"
            if name not in item:
                continue
            value = item[name]
            if value is None:
                raise ValueError("optional attributes must be omitted, not NULL")
            if field.name.endswith("_at") and value is not None:
                if not isinstance(value, str):
                    raise ValueError("timestamp must be text")
                value = utc_datetime(datetime.fromisoformat(value), field.name)
                if item[name] != timestamp(value):
                    raise ValueError("persisted timestamps must use fixed-width UTC encoding")
            elif field.name == "expected_date":
                value = date.fromisoformat(value)
            elif field.name in ("retry_count", "size_bytes", "enumerated_artifact_count",
                                "enumeration_failures", "acquisition_failures", "publication_failures") and value is not None:
                value = integer(value, field.name)
            elif field.name == "raw_provider_payload" and value is not None:
                value = json.loads(value, parse_constant=_reject_nonfinite_json)
                if not isinstance(value, dict):
                    raise ValueError("raw provider payload must be an object")
            values[field.name] = value
        result = model(**values)
        if isinstance(result, Artifact):
            if result.artifact_id != item.get("artifact_id"):
                raise ValueError("artifact identity mismatch")
            if (result.last_error is None) != (result.last_error_at is None):
                raise ValueError("error fields must be recorded together")
        for name in ("sec_url", "filing_index_url", "document_type", "content_type", "last_error"):
            value = getattr(result, name, None)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"invalid {name}")
        return result
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        raise RepositoryDataError(f"invalid {model.__name__} record: {exc}") from exc


def pending_sort(discovered_at: datetime, identity: str) -> str:
    return timestamp(discovered_at) + "/" + identity


def validate_pending(item, kind, identity):
    expected = {} if kind is None else {
        "pending_work_kind": kind,
        "pending_work_sort": pending_sort(datetime.fromisoformat(item["discovered_at"]), identity),
    }
    actual = {name: item[name] for name in ("pending_work_kind", "pending_work_sort") if name in item}
    if expected != actual:
        raise RepositoryDataError("pending-work attributes disagree with durable checkpoints")
    if len(identity.encode("utf-8")) > 2048:
        raise RepositoryDataError("DynamoDB partition key exceeds 2048 bytes")
    if kind is not None and len(expected["pending_work_sort"].encode("utf-8")) > 1024:
        raise RepositoryDataError("DynamoDB pending sort key exceeds 1024 bytes")
    if "filename" in item and len(item["filename"].encode("utf-8")) > 1024:
        raise RepositoryDataError("DynamoDB filename sort key exceeds 1024 bytes")
