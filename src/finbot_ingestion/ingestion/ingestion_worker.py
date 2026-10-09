"""S3 → database → SNS ordering, durable budgets and terminal delivery."""

import asyncio
import logging

from finbot_ingestion.domain.events import ArtifactReady
from finbot_ingestion.messaging.dead_letter import WorkFailure
from finbot_ingestion.repositories.errors import RepositoryConflict, RepositoryNotFound
from .work_control import Outcome, retryable, work_error

LOGGER = logging.getLogger(__name__)


class IngestionWorker:
    def __init__(self, discovery, downloader, publisher, dead_letter, filings, artifacts,
                 control, *, locks=None):
        self.discovery, self.downloader = discovery, downloader
        self.publisher, self.dead_letter = publisher, dead_letter
        self.filings, self.artifacts, self.control = filings, artifacts, control
        # Share the discovery registry so enumeration and terminal send cannot race.
        self.locks = locks if locks is not None else discovery.locks
        if self.locks is not discovery.locks:
            raise ValueError("worker and discovery must share identity locks")
        self._slots = asyncio.Semaphore(control.config.max_inflight_artifacts)
        self.metrics = None

    async def discover_company(self, company):
        accessions = await self.discovery.discover(company)
        for accession in accessions:
            await self.process_filing(accession)
        return accessions

    async def process_filing(self, accession_number):
        result = await self.enumerate_only(accession_number)
        if result.outcome == Outcome.TERMINAL:
            return result.outcome
        # Use known child identities, not an eventually consistent accession query.
        for artifact_id in result.artifact_ids:
            await self.process_artifact(artifact_id)
        return result.outcome

    async def enumerate_only(self, accession_number):
        result = await self.discovery.enumerate_filing(accession_number)
        if result.outcome == Outcome.TERMINAL:
            await self.send_dead_letter("filing", accession_number)
        return result

    async def process_artifact(self, artifact_id):
        async with self.locks.hold("artifact/" + artifact_id), self._slots:
            recovering = False
            while True:
                artifact = await self.artifacts.get(artifact_id)
                progress = await self.artifacts.get_checkpoint(artifact_id)
                if artifact is None or progress is None:
                    raise RepositoryNotFound(artifact_id)
                if artifact.published_at is not None:
                    return Outcome.COMPLETE
                if progress.terminal_at is not None:
                    return await self._send_dead_letter(self.artifacts, artifact_id, artifact, progress)
                filing = await self.filings.get(artifact.accession_number)
                parent = await self.filings.get_checkpoint(artifact.accession_number)
                if filing is None or parent is None:
                    raise RepositoryNotFound(artifact.accession_number)
                if parent.terminal_at is not None or parent.enumeration_completed_at is None:
                    return Outcome.DEFERRED
                stage = "ACQUIRE" if artifact.stored_at is None else "PUBLISH"
                try:
                    if stage == "ACQUIRE":
                        stored = await self.downloader.acquire(artifact)
                        await self.control.checkpoint(lambda: self.artifacts.mark_stored(artifact_id,
                            stored.s3_uri, stored.stored_at, stored.content_type, stored.size_bytes),
                            operation="mark_stored", identity=artifact_id)
                        if self.metrics is not None:
                            self.metrics.count("ArtifactsStored")
                            self.metrics.latency("DownloadLatencyMs", artifact.discovered_at, stored.stored_at)
                            self.metrics.latency("IngestionLatencyMs", filing.filed_at, stored.stored_at)
                        LOGGER.info("Artifact stored", extra={"operation": "ACQUIRE", "artifact_id": artifact_id,
                            "cik": artifact.company_cik, "accession_number": artifact.accession_number})
                    else:
                        try:
                            event = ArtifactReady.from_artifact(filing, artifact)
                        except ValueError as exc:
                            raise RepositoryConflict("stored child disagrees with canonical filing") from exc
                        await self.publisher.publish_artifact_ready(event)
                        published_at = self.control.now()
                        await self.control.checkpoint(lambda: self.artifacts.mark_published(artifact_id, published_at),
                            operation="mark_published", identity=artifact_id)
                        if self.metrics is not None:
                            self.metrics.count("ArtifactsPublished")
                        LOGGER.info("Artifact published", extra={"operation": "PUBLISH", "artifact_id": artifact_id,
                            "cik": artifact.company_cik, "accession_number": artifact.accession_number})
                    if recovering:
                        LOGGER.info("Workflow recovered", extra={"operation": stage, "artifact_id": artifact_id})
                        recovering = False
                except Exception as exc:
                    if stage == "PUBLISH" and self.metrics is not None:
                        self.metrics.count("ArtifactPublishFailures")
                    if not work_error(exc):
                        raise
                    # A checkpoint can have succeeded despite losing all acknowledgments.
                    current = await self.artifacts.get(artifact_id)
                    durable = await self.artifacts.get_checkpoint(artifact_id)
                    if current is None or durable is None:
                        raise RepositoryNotFound(artifact_id) from exc
                    if current.published_at is not None or (stage == "ACQUIRE" and current.stored_at is not None):
                        continue
                    if durable.terminal_at is not None:
                        continue
                    await self.control.failure(self.artifacts, artifact_id, stage, exc, durable)
                    durable = await self.artifacts.get_checkpoint(artifact_id)
                    if durable.terminal_at is None:
                        recovering = True
                        await self.control.backoff(durable.failures(stage))

    async def _send_dead_letter(self, repo, identity, source, progress):
        if progress.dead_lettered_at is not None:
            return Outcome.TERMINAL
        failure = WorkFailure.from_checkpoint(source, progress)
        for attempt in range(1, self.control.config.dead_letter_attempts + 1):
            try:
                await self.dead_letter.send(failure)
                sent_at = self.control.now()
                await self.control.checkpoint(lambda: repo.mark_dead_lettered(identity, sent_at),
                    operation="mark_dead_lettered", identity=identity)
                if self.metrics is not None:
                    self.metrics.count("DeadLetterMessages")
                LOGGER.warning("Terminal work dead-lettered", extra={"operation": "DEAD_LETTER",
                    "work_id": identity, "failure_id": failure.failure_id})
                return Outcome.TERMINAL
            except Exception as exc:
                durable = await repo.get_checkpoint(identity)
                if durable is not None and durable.dead_lettered_at is not None:
                    return Outcome.TERMINAL
                will_retry = retryable(exc) and attempt < self.control.config.dead_letter_attempts
                LOGGER.warning("Dead-letter send failed", extra={"operation": "DEAD_LETTER",
                    "work_id": identity, "error_type": type(exc).__name__, "attempt_number": attempt,
                    "will_retry": will_retry})
                if not will_retry:
                    raise
                await self.control.backoff(attempt)
        raise AssertionError("unreachable dead-letter loop")

    async def send_dead_letter(self, kind, identity):
        if kind not in ("filing", "artifact"):
            raise ValueError("dead-letter work must be filing or artifact")
        async with self.locks.hold(kind + "/" + identity):
            repo = self.filings if kind == "filing" else self.artifacts
            source, progress = await repo.get(identity), await repo.get_checkpoint(identity)
            if source is None or progress is None:
                raise RepositoryNotFound(identity)
            if progress.terminal_at is None:
                return Outcome.DEFERRED
            return await self._send_dead_letter(repo, identity, source, progress)
