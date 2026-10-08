from dataclasses import replace
from datetime import datetime

from finbot_ingestion.domain import Artifact
from finbot_ingestion.domain.checkpoints import ArtifactCheckpoint
from finbot_ingestion.domain.identity import artifact_identity, normalize_accession_number
from ..artifact_repository import WorkKind
from ..errors import RepositoryConflict
from ..package_checkpoint import assert_same_artifact
from ..pagination import Page
from .base import DynamoDBRepository
from .serialization import integer, pending_sort, record, restore, timestamp, validate_pending

PENDING_INDEX = "PendingArtifactWork"
ACCESSION_INDEX = "ArtifactsByAccession"


def normalize_artifact_id(value):
    if not isinstance(value, str) or value.count("/") != 1:
        raise ValueError("invalid artifact_id")
    accession, filename = value.split("/")
    return artifact_identity(accession, filename)


def work_kind(artifact: Artifact, progress: ArtifactCheckpoint | None = None) -> WorkKind | None:
    if progress is not None and progress.terminal_at is not None:
        return "DEAD_LETTER" if progress.pending_dead_letter else None
    if artifact.published_at is not None:
        return None
    return "ACQUIRE" if artifact.stored_at is None else "PUBLISH"


class DynamoDBArtifactRepository(DynamoDBRepository):
    def __init__(self, execution):
        super().__init__(execution, execution.config.artifacts_table, "artifact_id")

    @staticmethod
    def _validate(item):
        artifact = restore(Artifact, item)
        progress = restore(ArtifactCheckpoint, item)
        if progress.terminal_at is not None and (
                artifact.published_at is not None or progress.terminal_stage != work_kind(artifact)):
            raise RepositoryConflict("terminal checkpoint conflicts with artifact progress")
        if progress.publication_failures and artifact.stored_at is None:
            raise RepositoryConflict("publication failures require storage")
        integer(item.get("revision"), "revision")
        validate_pending(item, work_kind(artifact, progress), artifact.artifact_id)
        return artifact

    async def create_if_absent(self, artifact: Artifact) -> bool:
        if (artifact.stored_at is not None or artifact.published_at is not None
                or artifact.retry_count or artifact.last_error is not None):
            raise ValueError("creation requires a newly discovered artifact")
        item = {**record(artifact), "revision": 0,
                "pending_work_kind": "ACQUIRE",
                "pending_work_sort": pending_sort(artifact.discovered_at, artifact.artifact_id)}
        return await self._create(item, self._validate, assert_same_artifact)

    async def get(self, artifact_id: str) -> Artifact | None:
        item = await self._get(normalize_artifact_id(artifact_id))
        return None if item is None else self._validate(item)

    async def get_checkpoint(self, artifact_id: str) -> ArtifactCheckpoint | None:
        item = await self._get(normalize_artifact_id(artifact_id))
        if item is None:
            return None
        self._validate(item)
        return restore(ArtifactCheckpoint, item)

    async def mark_stored(self, artifact_id: str, s3_uri: str, stored_at: datetime,
                          content_type: str | None, size_bytes: int | None) -> None:
        stored_at = timestamp(stored_at)

        def decide(item):
            if "terminal_at" in item:
                raise RepositoryConflict("terminal artifact requires explicit operator redrive")
            current = self._validate(item)
            # Validate the proposed checkpoint even if the current record is stored.
            proposed = replace(current, s3_uri=s3_uri, stored_at=datetime.fromisoformat(stored_at),
                               content_type=content_type, size_bytes=size_bytes)
            if current.stored_at is not None:
                if (current.s3_uri, current.content_type, current.size_bytes) != (
                        s3_uri, content_type, size_bytes):
                    raise RepositoryConflict("conflicting storage checkpoint")
                return None
            sets = {"s3_uri": s3_uri, "stored_at": stored_at, "pending_work_kind": "PUBLISH"}
            removes = []
            for name in ("content_type", "size_bytes"):
                value = getattr(proposed, name)
                if value is None:
                    removes.append(name)
                else:
                    sets[name] = value
            return sets, removes
        await self._update(normalize_artifact_id(artifact_id), decide, self._validate)

    async def mark_published(self, artifact_id: str, published_at: datetime) -> None:
        published_at = timestamp(published_at)

        def decide(item):
            if "terminal_at" in item:
                raise RepositoryConflict("terminal artifact requires explicit operator redrive")
            if "stored_at" not in item:
                raise RepositoryConflict("publication requires durable storage")
            if "published_at" in item:
                return None
            return {"published_at": published_at}, ("pending_work_kind", "pending_work_sort")
        await self._update(normalize_artifact_id(artifact_id), decide, self._validate)

    async def record_failure(self, artifact_id: str, error: str, at: datetime) -> None:
        await self._failure(normalize_artifact_id(artifact_id), error, at,
                            self._validate, lambda item: "published_at" in item or "terminal_at" in item)

    async def record_stage_failure(self, artifact_id, stage, error, at, *, max_failures,
                                   error_type, terminal=False):
        if stage not in ("ACQUIRE", "PUBLISH"):
            raise ValueError("artifact failures must be ACQUIRE or PUBLISH")
        await self._stage_failure(normalize_artifact_id(artifact_id), stage, error, at,
                                 max_failures=max_failures, error_type=error_type, terminal=terminal,
                                 validate=self._validate,
                                 current_stage=lambda item: work_kind(restore(Artifact, item)))

    async def mark_dead_lettered(self, artifact_id, at):
        await self._mark_dead_lettered(normalize_artifact_id(artifact_id), at, self._validate)

    async def list_pending(self, kind: WorkKind, *, page_size=None, token=None) -> Page[Artifact]:
        if kind not in ("ACQUIRE", "PUBLISH", "DEAD_LETTER"):
            raise ValueError("kind must be ACQUIRE, PUBLISH or DEAD_LETTER")
        items, token = await self._query(index=PENDING_INDEX, partition="pending_work_kind",
                                         value=kind, page_size=page_size, token=token)
        artifacts = []
        for item in items:
            artifact = self._validate(item)
            if work_kind(artifact, restore(ArtifactCheckpoint, item)) == kind:
                artifacts.append(artifact)
        return Page(tuple(artifacts), token)

    async def list_for_filing(self, accession_number: str, *, page_size=None, token=None) -> Page[Artifact]:
        accession = normalize_accession_number(accession_number)
        items, token = await self._query(index=ACCESSION_INDEX, partition="accession_number",
                                         value=accession, page_size=page_size, token=token)
        artifacts = tuple(self._validate(item) for item in items)
        if any(a.accession_number != accession for a in artifacts):
            raise RepositoryConflict("index returned a different filing")
        return Page(artifacts, token)
