"""Commit an observed package only after every child record is durable.

No SEC fetching, storage, publication, or scheduling lives here. Completion records
the first validated snapshot, not a guarantee against later SEC additions.
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime

from finbot_ingestion.domain import Artifact, Filing
from finbot_ingestion.domain.identity import validate_filename
from finbot_ingestion.domain.validation import utc_datetime
from .artifact_repository import ArtifactRepository
from .filing_repository import FilingRepository
from .errors import RepositoryConflict, RepositoryNotFound


def assert_same_filing(existing: Filing, observed: Filing) -> None:
    for field in ("accession_number", "company_cik", "form_type", "filed_at", "filing_index_url"):
        if getattr(existing, field) != getattr(observed, field):
            raise RepositoryConflict(f"conflicting filing {field}")
    if (existing.primary_document_name is not None and observed.primary_document_name is not None
            and existing.primary_document_name != observed.primary_document_name):
        raise RepositoryConflict("conflicting filing primary_document_name")


def assert_same_artifact(existing: Artifact, observed: Artifact) -> None:
    for field in ("artifact_id", "accession_number", "company_cik", "form_type", "filename", "sec_url"):
        if getattr(existing, field) != getattr(observed, field):
            raise RepositoryConflict(f"conflicting artifact {field}")
    if (existing.document_type is not None and observed.document_type is not None
            and existing.document_type != observed.document_type):
        raise RepositoryConflict("conflicting artifact document_type")


class PackageCheckpoint:
    def __init__(self, filings: FilingRepository, artifacts: ArtifactRepository):
        self.filings, self.artifacts = filings, artifacts

    async def persist(self, filing: Filing, artifacts: Sequence[Artifact], *,
                      primary_document_name: str, completed_at: datetime) -> None:
        completed_at = utc_datetime(completed_at, "completed_at")
        validate_filename(primary_document_name)
        children = tuple(artifacts)
        if not children or len({a.artifact_id for a in children}) != len(children):
            raise ValueError("snapshot must contain unique artifacts")
        if primary_document_name not in {a.filename for a in children}:
            raise ValueError("snapshot must contain the primary document")
        for child in children:
            if (child.accession_number, child.company_cik, child.form_type) != (
                    filing.accession_number, filing.company_cik, filing.form_type):
                raise RepositoryConflict("snapshot child belongs to a different filing")
            if child.stored_at is not None or child.published_at is not None or child.retry_count:
                raise ValueError("snapshot inputs must be newly discovered artifacts")
        existing = await self.filings.get(filing.accession_number)
        if existing is None:
            raise RepositoryNotFound(filing.accession_number)
        assert_same_filing(existing, filing)
        if existing.primary_document_name not in (None, primary_document_name):
            raise RepositoryConflict("snapshot primary conflicts with filing")
        progress = await self.filings.get_checkpoint(filing.accession_number)
        if progress is not None and progress.terminal_at is not None:
            raise RepositoryConflict("terminal enumeration requires explicit operator redrive")
        if progress is not None and progress.resolved_primary_document_name not in (None, primary_document_name):
            raise RepositoryConflict("snapshot primary conflicts with checkpoint")
        for child in children:
            # A repeated discovery may carry today's ticker. Children must agree
            # with the original parent for the unchanged ArtifactReady contract.
            child = replace(child, ticker=existing.ticker)
            await self.artifacts.create_if_absent(child)
            durable = await self.artifacts.get(child.artifact_id)
            if durable is None:
                raise RepositoryNotFound(child.artifact_id)
            assert_same_artifact(durable, child)
            if durable.ticker != existing.ticker:
                raise RepositoryConflict("durable child ticker disagrees with canonical filing")
        await self.filings.mark_enumerated(
            filing.accession_number, primary_document_name=primary_document_name,
            artifact_count=len(children), completed_at=completed_at,
        )
