from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta

from finbot_ingestion.domain import ExpectedEarningsEvent
from finbot_ingestion.domain.validation import utc_datetime
from finbot_ingestion.calendar.contracts import CalendarSyncRun, CalendarSyncState, provider_name
from ..errors import RepositoryBusy, RepositoryConflict, RepositoryDataError
from .base import DynamoDBRepository
from .serialization import decode, encode, record, restore
from .calendar_state import SYNC_PARTITION, read_state, state_record


def active_event(item):
    event = restore(ExpectedEarningsEvent, item)
    if type(item.get("calendar_active", True)) is not bool:
        raise RepositoryDataError("calendar_active must be boolean")
    return event, item.get("calendar_active", True)


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
                    ConditionExpression="attribute_not_exists(#date) OR #synced < :synced AND #provider = :provider",
                    ExpressionAttributeNames={"#date": "expected_date", "#synced": "synced_at", "#provider": "provider"},
                    ExpressionAttributeValues=encode({":synced": item["synced_at"], ":provider": item["provider"]}))
            except self.execution.client.exceptions.ConditionalCheckFailedException:
                response = await self._call("get_item", Key=encode({
                    "expected_date": item["expected_date"], "cik": item["cik"]}), ConsistentRead=True)
                if "Item" not in response:
                    raise RepositoryBusy("calendar conflict disappeared")
                existing = decode(response["Item"])
                _, active = active_event(existing)
                if existing["provider"] != item["provider"]:
                    raise RepositoryConflict("calendar provider replacement requires explicit reconciliation")
                if existing["synced_at"] < item["synced_at"]:
                    raise RepositoryBusy("calendar conditional write was not resolved")
                if existing["synced_at"] == item["synced_at"] and (not active or existing != item):
                    raise RepositoryConflict("different calendar observations share synced_at")

    async def cancel_event(self, event: ExpectedEarningsEvent, *, at: datetime) -> bool:
        """Keep an observation tombstone so delayed upserts cannot resurrect it."""
        at = utc_datetime(at, "at")
        key = encode({"expected_date": event.expected_date.isoformat(), "cik": event.company_cik})
        for _ in range(self.execution.config.cas_attempts):
            response = await self._call("get_item", Key=key, ConsistentRead=True)
            if "Item" not in response:
                raise RepositoryConflict("calendar cancellation source disappeared")
            item = decode(response["Item"])
            current, active = active_event(item)
            if (current.company_cik != event.company_cik or current.expected_date != event.expected_date
                    or current.provider != event.provider):
                raise RepositoryConflict("calendar cancellation provider changed")
            if current.synced_at > at:
                return False
            if not active and current.synced_at == at:
                return True  # Repair an ambiguous acknowledgment for this cancellation.
            if current.synced_at == at:
                raise RepositoryConflict("active and cancelled observations share synced_at")
            proposed = {**item, "synced_at": record(replace(current, synced_at=at))["synced_at"],
                        "calendar_active": False}
            try:
                await self._call("put_item", Item=encode(proposed),
                    ConditionExpression="attribute_exists(#date) AND #synced = :old",
                    ExpressionAttributeNames={"#date": "expected_date", "#synced": "synced_at"},
                    ExpressionAttributeValues=encode({":old": item["synced_at"]}))
                return active
            except self.execution.client.exceptions.ConditionalCheckFailedException:
                continue
        raise RepositoryBusy("calendar cancellation attempts exhausted")

    async def get_sync_state(self, provider: str) -> CalendarSyncState | None:
        provider_name(provider)
        response = await self._call("get_item", Key=encode({"expected_date": SYNC_PARTITION, "cik": provider}),
                                    ConsistentRead=True)
        state = read_state(decode(response["Item"])) if "Item" in response else None
        if state is not None and state.provider != provider:
            raise RepositoryDataError("sync query returned a different provider")
        return state

    async def _change_sync(self, provider, decide):
        for _ in range(self.execution.config.cas_attempts):
            current = await self.get_sync_state(provider)
            proposed, result = decide(current)
            if proposed is None:
                return result
            names = {"#date": "expected_date"}
            params = {}
            condition = "attribute_not_exists(#date)"
            if current is not None:
                names["#rev"] = "revision"
                condition = "attribute_exists(#date) AND #rev = :old"
                params["ExpressionAttributeValues"] = encode({":old": current.revision})
            try:
                await self._call("put_item", Item=encode(state_record(proposed)), ConditionExpression=condition,
                                 ExpressionAttributeNames=names, **params)
                return result
            except self.execution.client.exceptions.ConditionalCheckFailedException:
                continue
        raise RepositoryBusy("calendar sync state attempts exhausted")

    async def begin_sync(self, run: CalendarSyncRun) -> CalendarSyncRun:
        """Same request ID repairs ambiguous acknowledgments, not a second run."""
        def decide(current):
            if current is not None and current.latest_run.request_id == run.request_id:
                canonical = replace(run, observed_at=current.latest_run.observed_at)
                if canonical != current.latest_run:
                    raise RepositoryConflict("sync request ID has conflicting scope")
                return None, current.latest_run
            at = run.observed_at
            if current is not None:
                at = max(at, current.latest_run.observed_at + timedelta(microseconds=1))
            canonical = replace(run, observed_at=at)
            state = (CalendarSyncState(provider=run.provider, revision=0, latest_run=canonical) if current is None
                     else replace(current, revision=current.revision + 1, latest_run=canonical))
            return state, canonical
        return await self._change_sync(run.provider, decide)

    async def complete_sync(self, success) -> None:
        def decide(current):
            if current is None or current.latest_run != success.run:
                raise RepositoryConflict("sync completion does not match latest run")
            name = success.run.kind + "_success"
            existing = getattr(current, name)
            if existing is not None and existing.run == success.run:
                if existing != success:
                    raise RepositoryConflict("different completion facts for one sync")
                return None, None
            changes = {name: success, "revision": current.revision + 1}
            if current.failure_run is not None and current.failure_run.kind == success.run.kind:
                changes.update(failure_run=None, last_error=None, last_error_at=None)
            return replace(current, **changes), None
        await self._change_sync(success.run.provider, decide)

    async def fail_sync(self, run: CalendarSyncRun, *, error: str, at: datetime) -> None:
        at = utc_datetime(at, "at")
        def decide(current):
            if current is None or current.latest_run != run:
                raise RepositoryConflict("sync failure does not match latest run")
            success = getattr(current, run.kind + "_success")
            if success is not None and success.run == run:
                return None, None
            if current.failure_run == run and current.last_error == error and current.last_error_at == at:
                return None, None
            return replace(current, revision=current.revision + 1, failure_run=run,
                           last_error=error, last_error_at=at), None
        await self._change_sync(run.provider, decide)

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
                    event, active = active_event(item)
                    if event.expected_date.isoformat() != day:
                        raise RepositoryConflict("calendar query returned a different date")
                    if active:
                        result.append(event)
                if token is None:
                    break
        return result
