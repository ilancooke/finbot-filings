"""Conditional satisfaction records in a reserved Calendar-table partition."""

from dataclasses import fields, replace
from datetime import date, datetime
import json

from finbot_ingestion.domain.satisfaction import EventIdentity, EventSatisfaction
from ..errors import RepositoryConflict, RepositoryDataError, RepositoryBusy
from .base import DynamoDBRepository
from .serialization import encode, decode, integer, timestamp
from finbot_ingestion.ingestion.work_control import retryable

SATISFACTION_PARTITION = "__event_satisfaction__"


def satisfaction_record(value):
    item = {"repository_schema_version": 1, "calendar_record_type": "satisfaction",
            "expected_date": SATISFACTION_PARTITION, "cik": value.identity.key}
    for field in fields(value):
        supplied = getattr(value, field.name)
        if field.name == "identity" or supplied is None:
            continue
        if isinstance(supplied, datetime):
            supplied = timestamp(supplied)
        elif type(supplied) is date:
            supplied = supplied.isoformat()
        elif isinstance(supplied, tuple):
            supplied = list(supplied)
        item[field.name] = supplied
    return item


def read_satisfaction(item):
    try:
        if (integer(item["repository_schema_version"], "version") != 1 or
                item["calendar_record_type"] != "satisfaction" or item["expected_date"] != SATISFACTION_PARTITION):
            raise ValueError("invalid satisfaction record")
        identity = EventIdentity(*json.loads(item["cik"]))
        if item["match_policy_version"] is None or item["match_reason"] is None:
            raise ValueError("policy version and reason are required")
        if identity.key != item["cik"]:
            raise ValueError("noncanonical identity")
        values = {f.name: item[f.name] for f in fields(EventSatisfaction) if f.name in item}
        for name in ("matched_filed_at", "satisfied_at", "window_start", "window_end", "grace_end"):
            values[name] = datetime.fromisoformat(item[name])
            if timestamp(values[name]) != item[name]:
                raise ValueError("noncanonical timestamp")
        values["observed_expected_date"] = date.fromisoformat(item["observed_expected_date"])
        values["matched_sec_items"] = tuple(item.get("matched_sec_items", ()))
        return EventSatisfaction(identity=identity, **values)
    except (TypeError, ValueError, KeyError) as exc:
        raise RepositoryDataError("invalid earnings satisfaction checkpoint") from exc


class DynamoDBSatisfactionRepository(DynamoDBRepository):
    def __init__(self, execution, filings):
        super().__init__(execution, execution.config.calendar_table, "expected_date")
        self.filings = filings

    async def get(self, identity):
        response = await self._call("get_item", Key=encode({"expected_date": SATISFACTION_PARTITION,
            "cik": identity.key}), ConsistentRead=True)
        if "Item" not in response:
            return None
        result = read_satisfaction(decode(response["Item"]))
        if result.identity != identity:
            raise RepositoryDataError("satisfaction identity differs from query")
        return result

    async def create_if_absent(self, value):
        # Revalidate even if a caller bypassed the frozen dataclass initializer.
        item = satisfaction_record(value)
        read_satisfaction(item)
        filing = await self.filings.get(value.matched_accession_number)
        if filing is None or (filing.company_cik, filing.form_type, filing.filed_at) != (
                value.identity.company_cik, value.matched_form_type, value.matched_filed_at):
            raise RepositoryConflict("satisfaction requires matching durable filing")
        try:
            await self._call("put_item", Item=encode(item),
                ConditionExpression="attribute_not_exists(#key)", ExpressionAttributeNames={"#key": "expected_date"})
            return True
        except Exception as exc:
            conditional = isinstance(exc, self.execution.client.exceptions.ConditionalCheckFailedException)
            if not conditional and not retryable(exc):
                raise
            current = await self.get(value.identity)
            if current is None:
                if conditional:
                    raise RepositoryBusy("satisfaction conflict disappeared") from exc
                raise
            # Compatible replay preserves the original observation time and bounds.
            if replace(value, satisfied_at=current.satisfied_at) != current:
                raise RepositoryConflict("conflicting earnings satisfaction facts")
            return False
