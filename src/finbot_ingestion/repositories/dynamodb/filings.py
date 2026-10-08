from datetime import datetime

from finbot_ingestion.domain import Filing
from finbot_ingestion.domain.checkpoints import FilingCheckpoint
from finbot_ingestion.domain.identity import normalize_accession_number
from ..errors import RepositoryConflict
from ..package_checkpoint import assert_same_filing
from ..pagination import Page
from .base import DynamoDBRepository
from .serialization import integer, pending_sort, record, restore, validate_pending

PENDING_INDEX = "PendingFilingEnumeration"


class DynamoDBFilingRepository(DynamoDBRepository):
    def __init__(self, execution):
        super().__init__(execution, execution.config.filings_table, "accession_number")

    @staticmethod
    def _validate(item):
        filing = restore(Filing, item)
        progress = restore(FilingCheckpoint, item)
        integer(item.get("revision"), "revision")
        validate_pending(item, "ENUMERATE" if progress.enumeration_completed_at is None else None,
                         filing.accession_number)
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
                              artifact_count: int, completed_at: datetime) -> None:
        """Low-level commit; callers use PackageCheckpoint to confirm child durability."""
        key = normalize_accession_number(accession_number)
        progress = FilingCheckpoint(accession_number=key, enumeration_completed_at=completed_at,
                                    resolved_primary_document_name=primary_document_name,
                                    enumerated_artifact_count=artifact_count)

        def decide(item):
            if item.get("primary_document_name") not in (None, primary_document_name):
                raise RepositoryConflict("enumerated primary conflicts with original filing")
            if "enumeration_completed_at" in item:
                if item["resolved_primary_document_name"] != primary_document_name:
                    raise RepositoryConflict("enumerated primary conflicts with checkpoint")
                # The first completed snapshot's timestamp/count are immutable.
                return None
            return ({name: value for name, value in record(progress).items()
                     if name in ("enumeration_completed_at", "resolved_primary_document_name", "enumerated_artifact_count")},
                    ("pending_work_kind", "pending_work_sort"))
        await self._update(key, decide, self._validate)

    async def record_failure(self, accession_number: str, error: str, at: datetime) -> None:
        await self._failure(normalize_accession_number(accession_number), error, at,
                            self._validate, lambda item: "enumeration_completed_at" in item)

    async def list_pending(self, *, page_size=None, token=None) -> Page[Filing]:
        items, token = await self._query(index=PENDING_INDEX, partition="pending_work_kind",
                                         value="ENUMERATE", page_size=page_size, token=token)
        filings = []
        for item in items:
            filing = self._validate(item)
            if "enumeration_completed_at" not in item:
                filings.append(filing)
        return Page(tuple(filings), token)
