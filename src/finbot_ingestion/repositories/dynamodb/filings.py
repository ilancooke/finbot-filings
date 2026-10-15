from datetime import datetime

from finbot_ingestion.domain import Filing
from finbot_ingestion.domain.checkpoints import FilingCheckpoint
from finbot_ingestion.domain.identity import normalize_accession_number
from finbot_ingestion.domain.filing_ready import FilingReady
from finbot_ingestion.messaging.publisher import bounded_json_message
from ..errors import RepositoryConflict, RepositoryDataError
from ..package_checkpoint import assert_same_filing
from ..pagination import Page
from .base import DynamoDBRepository
from .serialization import encode, integer, pending_sort, record, restore, timestamp, validate_pending

PENDING_INDEX = "PendingFilingEnumeration"


def work_kind(progress):
    if progress.terminal_at is not None:
        return "DEAD_LETTER" if progress.pending_dead_letter else None
    if progress.selection_blocked:
        return None
    if progress.enumeration_completed_at is None:
        return "ENUMERATE"
    if progress.selected_artifact_ids is not None and progress.ready_published_at is None:
        return "PUBLISH"
    return None


class DynamoDBFilingRepository(DynamoDBRepository):
    def __init__(self, execution):
        super().__init__(execution, execution.config.filings_table, "accession_number")

    @staticmethod
    def _validate(item):
        filing = restore(Filing, item)
        progress = restore(FilingCheckpoint, item)
        integer(item.get("revision"), "revision")
        validate_pending(item, work_kind(progress), filing.accession_number)
        if (filing.primary_document_name is not None and progress.resolved_primary_document_name is not None
                and filing.primary_document_name != progress.resolved_primary_document_name):
            raise RepositoryConflict("resolved primary conflicts with original filing")
        return filing

    async def create_if_absent(self, filing: Filing) -> bool:
        item = {**record(filing), "revision": 0, "retry_count": 0,
                "pending_work_kind": "ENUMERATE",
                "pending_work_sort": pending_sort(filing.discovered_at, filing.accession_number)}
        return await self._create(item, self._validate, assert_same_filing)

    async def get(self, accession_number: str) -> Filing | None:
        item = await self._get(normalize_accession_number(accession_number))
        return None if item is None else self._validate(item)

    async def exists(self, accession_number: str) -> bool:
        return await self.get(accession_number) is not None

    async def get_checkpoint(self, accession_number: str) -> FilingCheckpoint | None:
        item = await self._get(normalize_accession_number(accession_number))
        if item is None:
            return None
        self._validate(item)
        return restore(FilingCheckpoint, item)

    async def mark_enumerated(self, accession_number: str, *, primary_document_name: str,
                              artifact_count: int, completed_at: datetime, artifact_ids=None) -> None:
        """Low-level commit; callers use PackageCheckpoint to confirm child durability."""
        key = normalize_accession_number(accession_number)
        if artifact_ids is not None and len(artifact_ids) != artifact_count:
            raise ValueError("artifact_count must match the selected membership")
        progress = FilingCheckpoint(accession_number=key, enumeration_completed_at=completed_at,
                                    resolved_primary_document_name=primary_document_name,
                                    enumerated_artifact_count=artifact_count,
                                    selected_artifact_ids=artifact_ids)

        def decide(item):
            if restore(FilingCheckpoint, item).selection_blocked:
                raise RepositoryConflict("excluded/unresolved filing cannot be enumerated")
            if "terminal_at" in item:
                raise RepositoryConflict("terminal enumeration requires explicit operator redrive")
            if item.get("primary_document_name") not in (None, primary_document_name):
                raise RepositoryConflict("enumerated primary conflicts with original filing")
            if "enumeration_completed_at" in item:
                if item["resolved_primary_document_name"] != primary_document_name:
                    raise RepositoryConflict("enumerated primary conflicts with checkpoint")
                # Keep the first enumeration facts, but explicit package recovery
                # can add members and produce a new complete filing notification.
                if artifact_ids is None:
                    return None
                old = set(item.get("selected_artifact_ids", ()))
                members = sorted(old | set(artifact_ids))
                if members == item.get("selected_artifact_ids"):
                    return None
                if item.get("ready_snapshot_id") and "ready_published_at" not in item:
                    raise RepositoryConflict("publish the saved snapshot before adding documents")
                return ({"selected_artifact_ids": members, "pending_work_kind": "PUBLISH",
                         "pending_work_sort": pending_sort(datetime.fromisoformat(item["discovered_at"]), key)},
                        ("ready_snapshot_id", "ready_published_at"))
            sets = {name: value for name, value in record(progress).items()
                    if name in ("enumeration_completed_at", "resolved_primary_document_name",
                                "enumerated_artifact_count", "selected_artifact_ids")}
            if artifact_ids is not None:
                sets["pending_work_kind"] = "PUBLISH"
                return sets, ()
            return sets, ("pending_work_kind", "pending_work_sort")
        await self._update(key, decide, self._validate)

    @staticmethod
    def _ready_key(accession, snapshot_id):
        return f"__filing_ready__/{accession}/{snapshot_id}"

    @staticmethod
    def _restore_ready(row):
        try:
            if row.get("record_type") != "filing_ready" or integer(row.get("repository_schema_version"), "schema version") != 1:
                raise ValueError("unknown notification record")
            integer(row.get("revision"), "revision")
            if not isinstance(row.get("event_json"), str):
                raise ValueError("notification JSON must be a string")
            event = FilingReady.from_json(bounded_json_message(row["event_json"]))
            if row["accession_number"] != DynamoDBFilingRepository._ready_key(event.filing_id, event.snapshot_id):
                raise ValueError("notification record key mismatch")
            if "published_at" in row and timestamp(datetime.fromisoformat(row["published_at"])) != row["published_at"]:
                raise ValueError("notification publication timestamp must use fixed-width UTC")
            return event
        except (ValueError, KeyError, TypeError) as exc:
            raise RepositoryDataError("invalid saved filing notification") from exc

    async def get_ready_event(self, accession_number):
        progress = await self.get_checkpoint(accession_number)
        if progress is None or progress.ready_snapshot_id is None:
            return None
        row = await self._get(self._ready_key(accession_number, progress.ready_snapshot_id))
        if row is None:
            raise RepositoryConflict("saved filing notification is missing")
        event = self._restore_ready(row)
        if (event.filing_id, event.snapshot_id) != (accession_number, progress.ready_snapshot_id):
            raise RepositoryConflict("saved filing notification identity mismatch")
        return event

    async def save_ready_event(self, event):
        """Save immutable bytes first; attach only to the matching durable selection."""
        try:
            message = bounded_json_message(event.to_json())
        except ValueError as exc:
            raise RepositoryConflict(str(exc)) from exc
        key = self._ready_key(event.filing_id, event.snapshot_id)
        row = {"accession_number": key, "repository_schema_version": 1,
               "event_json": message, "record_type": "filing_ready", "revision": 0}
        try:
            await self._call("put_item", Item=encode(row), ConditionExpression="attribute_not_exists(#key)",
                             ExpressionAttributeNames={"#key": "accession_number"})
        except self.execution.client.exceptions.ConditionalCheckFailedException:
            existing = await self._get(key)
            if existing is None:
                raise RepositoryConflict("saved notification disappeared")
            canonical = self._restore_ready(existing)
            if canonical.manifest() != event.manifest():
                raise RepositoryConflict("conflicting filing notification")
            event = canonical

        def decide(item):
            filing, progress = self._validate(item), restore(FilingCheckpoint, item)
            if progress.terminal_at is not None or progress.selection_blocked:
                raise RepositoryConflict("blocked filing cannot become ready")
            if (tuple(child.artifact_id for child in event.artifacts) != progress.selected_artifact_ids
                    or event.primary_artifact_id != event.filing_id + "/" + progress.resolved_primary_document_name
                    or (event.cik, event.ticker, event.form_type, event.filed_at, event.company_name) !=
                       (filing.company_cik, filing.ticker, filing.form_type, filing.filed_at, filing.company_name)):
                raise RepositoryConflict("notification disagrees with canonical filing selection")
            if progress.ready_snapshot_id is not None:
                if progress.ready_snapshot_id != event.snapshot_id:
                    raise RepositoryConflict("filing already has a different ready snapshot")
                return None
            return {"ready_snapshot_id": event.snapshot_id}, ()
        await self._update(event.filing_id, decide, self._validate)
        return event

    async def mark_ready_published(self, accession_number, snapshot_id, at):
        at = timestamp(at)
        current = await self.get_checkpoint(accession_number)
        if (current is None or current.ready_snapshot_id != snapshot_id
                or current.terminal_at is not None or current.selection_blocked):
            raise RepositoryConflict("publication requires the current unblocked saved snapshot")
        # Preserve each snapshot's first publication acknowledgment, including
        # after a later attachment creates a new current snapshot on the parent.
        key = self._ready_key(accession_number, snapshot_id)
        def archive(item):
            return None if "published_at" in item else ({"published_at": at}, ())
        await self._update(key, archive, self._restore_ready)
        archived = await self._get(key)
        at = archived["published_at"]
        def decide(item):
            progress = restore(FilingCheckpoint, item)
            if progress.terminal_at is not None or progress.selection_blocked:
                raise RepositoryConflict("blocked filing cannot be published")
            if progress.ready_snapshot_id != snapshot_id:
                raise RepositoryConflict("publication requires the saved snapshot")
            if progress.ready_published_at is not None:
                return None
            return {"ready_published_at": at}, ("pending_work_kind", "pending_work_sort")
        await self._update(normalize_accession_number(accession_number), decide, self._validate)

    async def freeze_company_name(self, accession_number, name):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("company name must be nonempty")
        def decide(item):
            return None if item.get("company_name") is not None else ({"company_name": name.strip()}, ())
        await self._update(normalize_accession_number(accession_number), decide, self._validate)

    async def record_failure(self, accession_number: str, error: str, at: datetime) -> None:
        await self._failure(normalize_accession_number(accession_number), error, at,
                            self._validate, lambda item: "enumeration_completed_at" in item or "terminal_at" in item
                            or restore(FilingCheckpoint, item).selection_blocked)

    async def record_selection(self, accession_number, decision, at, *, policy_version):
        await self._record_selection(normalize_accession_number(accession_number), decision, at,
                                     policy_version=policy_version, validate=self._validate)

    async def record_stage_failure(self, accession_number, stage, error, at, *, max_failures,
                                   error_type, terminal=False):
        if stage not in ("ENUMERATE", "PUBLISH"):
            raise ValueError("filing failures must be ENUMERATE or PUBLISH")
        await self._stage_failure(normalize_accession_number(accession_number), stage, error, at,
                                 max_failures=max_failures, error_type=error_type, terminal=terminal,
                                 validate=self._validate,
                                 current_stage=lambda item: work_kind(restore(FilingCheckpoint, item)))

    async def mark_dead_lettered(self, accession_number, at):
        await self._mark_dead_lettered(normalize_accession_number(accession_number), at, self._validate)

    async def list_pending(self, *, kind="ENUMERATE", page_size=None, token=None) -> Page[Filing]:
        if kind not in ("ENUMERATE", "PUBLISH", "DEAD_LETTER"):
            raise ValueError("filing work kind must be ENUMERATE, PUBLISH or DEAD_LETTER")
        items, token = await self._query(index=PENDING_INDEX, partition="pending_work_kind",
                                         value=kind, page_size=page_size, token=token)
        filings = []
        for item in items:
            filing = self._validate(item)
            if work_kind(restore(FilingCheckpoint, item)) == kind:
                filings.append(filing)
        return Page(tuple(filings), token)
