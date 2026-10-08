from collections.abc import Sequence
from datetime import datetime, timedelta

from finbot_ingestion.domain import ExpectedEarningsEvent
from finbot_ingestion.domain.validation import utc_datetime
from ..errors import RepositoryBusy, RepositoryConflict
from .base import DynamoDBRepository
from .serialization import decode, encode, record, restore


class DynamoDBCalendarRepository(DynamoDBRepository):
    def __init__(self, execution):
        super().__init__(execution, execution.config.calendar_table, "expected_date")

    async def upsert_events(self, events: Sequence[ExpectedEarningsEvent]) -> None:
        # Validate the whole supplied batch before any writes. Writes remain per-row.
        items = [record(event) for event in events]
        for item in items:
            restore(ExpectedEarningsEvent, item)
            encode(item)
        for item in items:
            try:
                await self._call("put_item", Item=encode(item),
                    ConditionExpression="attribute_not_exists(#date) OR #synced < :synced",
                    ExpressionAttributeNames={"#date": "expected_date", "#synced": "synced_at"},
                    ExpressionAttributeValues=encode({":synced": item["synced_at"]}))
            except self.execution.client.exceptions.ConditionalCheckFailedException:
                response = await self._call("get_item", Key=encode({
                    "expected_date": item["expected_date"], "cik": item["cik"]}), ConsistentRead=True)
                if "Item" not in response:
                    raise RepositoryBusy("calendar conflict disappeared")
                existing = decode(response["Item"])
                restore(ExpectedEarningsEvent, existing)
                if existing["synced_at"] < item["synced_at"]:
                    raise RepositoryBusy("calendar conditional write was not resolved")
                if existing["synced_at"] == item["synced_at"] and existing != item:
                    raise RepositoryConflict("different calendar observations share synced_at")

    async def get_events(self, start: datetime, end: datetime) -> list[ExpectedEarningsEvent]:
        start, end = utc_datetime(start, "start"), utc_datetime(end, "end")
        if start > end:
            raise ValueError("start must not follow end")
        days = (end.date() - start.date()).days + 1
        if days > self.execution.config.max_calendar_range_days:
            raise ValueError("calendar range exceeds configured maximum")
        result = []
        for offset in range(days):
            day = (start.date() + timedelta(days=offset)).isoformat()
            token = None
            while True:
                items, token = await self._query(partition="expected_date", value=day,
                                                 token=token, hydrate=False)
                for item in items:
                    event = restore(ExpectedEarningsEvent, item)
                    if event.expected_date.isoformat() != day:
                        raise RepositoryConflict("calendar query returned a different date")
                    result.append(event)
                if token is None:
                    break
        return result
