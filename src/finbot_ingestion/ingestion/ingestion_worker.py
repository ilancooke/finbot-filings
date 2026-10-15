"""Per-file acquisition and whole-filing publication with durable recovery."""

import asyncio
import logging

from finbot_ingestion.domain.filing_ready import FilingReady
from finbot_ingestion.messaging.dead_letter import WorkFailure
from finbot_ingestion.repositories.errors import RepositoryConflict, RepositoryNotFound
from .work_control import Outcome, retryable, work_error

LOGGER = logging.getLogger(__name__)


class IngestionWorker:
    def __init__(self, discovery, downloader, publisher, dead_letter, filings, artifacts,
                 control, *, locks=None, companies=None):
        self.companies = companies
        self.discovery, self.downloader = discovery, downloader
        self.discovery.downloader = downloader
        self.publisher, self.dead_letter = publisher, dead_letter
        self.filings, self.artifacts, self.control = filings, artifacts, control
        # Share the discovery registry so enumeration and terminal send cannot race.
        self.locks = locks if locks is not None else discovery.locks
        if self.locks is not discovery.locks:
            raise ValueError("worker and discovery must share identity locks")
        self._slots = asyncio.Semaphore(control.config.max_inflight_artifacts)
        self.discovery.artifact_slots = self._slots
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
        return await self.publish_filing(accession_number)

    async def enumerate_only(self, accession_number):
        result = await self.discovery.enumerate_filing(accession_number)
        if result.outcome == Outcome.TERMINAL:
            await self.send_dead_letter("filing", accession_number)
        return result

    async def process_artifact(self, artifact_id):
        outcome = await self._store_artifact(artifact_id)
        if outcome == Outcome.COMPLETE:
            artifact = await self.artifacts.get(artifact_id)
            result = await self.publish_filing(artifact.accession_number)
            if result == Outcome.TERMINAL:
                return result
        return outcome

    async def _store_artifact(self, artifact_id):
        async with self.locks.hold("artifact/" + artifact_id), self._slots:
            while True:
                artifact = await self.artifacts.get(artifact_id)
                progress = await self.artifacts.get_checkpoint(artifact_id)
                if artifact is None or progress is None:
                    raise RepositoryNotFound(artifact_id)
                if artifact.published_at is not None:
                    return Outcome.COMPLETE
                if progress.terminal_at is not None:
                    return await self._send_dead_letter(self.artifacts, artifact_id, artifact, progress)
                if progress.selection_blocked:
                    return Outcome.SKIPPED
                filing = await self.filings.get(artifact.accession_number)
                parent = await self.filings.get_checkpoint(artifact.accession_number)
                if filing is None or parent is None:
                    raise RepositoryNotFound(artifact.accession_number)
                if parent.terminal_at is not None:
                    return Outcome.DEFERRED
                if parent.selection_blocked:
                    await self.discovery.authorize_artifact(artifact, filing, parent, progress)
                    return Outcome.SKIPPED
                if parent.enumeration_completed_at is None:
                    # Even partially enumerated legacy children must not linger
                    # forever when their parent is outside the live cutoff.
                    if (self.discovery.selection is not None and
                            filing.filed_at < self.discovery.selection.live_start_at):
                        await self.discovery.authorize_artifact(artifact, filing, parent, progress)
                        return Outcome.SKIPPED
                    return Outcome.DEFERRED
                stage = "ACQUIRE"
                try:
                    if not await self.discovery.authorize_artifact(artifact, filing, parent, progress):
                        return Outcome.SKIPPED
                    # Routing may have stored the primary's already fetched bytes.
                    artifact = await self.artifacts.get(artifact_id)
                    stage = "ACQUIRE"
                    if artifact.stored_at is None:
                        stored = await self.downloader.acquire(artifact, filing)
                        await self.control.checkpoint(lambda: self.artifacts.mark_stored(artifact_id,
                            stored.s3_uri, stored.stored_at, stored.content_type, stored.size_bytes),
                            operation="mark_stored", identity=artifact_id)
                        if self.metrics is not None:
                            self.metrics.count("ArtifactsStored")
                            self.metrics.latency("DownloadLatencyMs", artifact.discovered_at, stored.stored_at)
                            self.metrics.latency("IngestionLatencyMs", filing.filed_at, stored.stored_at)
                        LOGGER.info("Artifact stored", extra={"operation": "ACQUIRE", "artifact_id": artifact_id,
                            "cik": artifact.company_cik, "accession_number": artifact.accession_number})
                    # Storage is per file; publication belongs to the whole filing.
                    return Outcome.COMPLETE
                except Exception as exc:
                    if not work_error(exc):
                        raise
                    # A checkpoint can have succeeded despite losing all acknowledgments.
                    current = await self.artifacts.get(artifact_id)
                    durable = await self.artifacts.get_checkpoint(artifact_id)
                    if current is None or durable is None:
                        raise RepositoryNotFound(artifact_id) from exc
                    if durable.selection_blocked:
                        return Outcome.SKIPPED
                    if current.published_at is not None or (stage == "ACQUIRE" and current.stored_at is not None):
                        continue
                    if durable.terminal_at is not None:
                        continue
                    await self.control.failure(self.artifacts, artifact_id, stage, exc, durable)
                    durable = await self.artifacts.get_checkpoint(artifact_id)
                    if durable.terminal_at is None:
                        await self.control.backoff(durable.failures(stage))

    async def publish_filing(self, accession_number):
        await self._ensure_membership(accession_number)
        async with self.locks.hold("filing/" + accession_number):
            while True:
                filing = await self.filings.get(accession_number)
                progress = await self.filings.get_checkpoint(accession_number)
                if filing is None or progress is None:
                    raise RepositoryNotFound(accession_number)
                if progress.terminal_at is not None:
                    return await self._send_dead_letter(self.filings, accession_number, filing, progress)
                if progress.selection_blocked:
                    return Outcome.SKIPPED
                if progress.enumeration_completed_at is None or progress.selected_artifact_ids is None:
                    return Outcome.DEFERRED
                if (self.discovery.selection is not None
                        and filing.filed_at < self.discovery.selection.live_start_at):
                    return Outcome.SKIPPED
                children = []
                for identity in progress.selected_artifact_ids:
                    child = await self.artifacts.get(identity)
                    state = await self.artifacts.get_checkpoint(identity)
                    if child is None or state is None or child.stored_at is None:
                        return Outcome.DEFERRED
                    if state.terminal_at is not None or state.selection_blocked:
                        return Outcome.DEFERRED
                    children.append(child)
                if progress.ready_published_at is not None:
                    await self._mark_children_published(children, progress.ready_published_at)
                    return Outcome.COMPLETE
                try:
                    event = await self.filings.get_ready_event(accession_number)
                    if event is None:
                        if filing.company_name is None and self.companies is not None:
                            company = await self.companies.get(filing.company_cik)
                            if company is not None:
                                await self.filings.freeze_company_name(accession_number, company.name)
                                filing = await self.filings.get(accession_number)
                        try:
                            event = FilingReady.from_artifacts(filing, children,
                                primary_document_name=progress.resolved_primary_document_name,
                                company_name=filing.company_name, ready_at=self.control.now())
                        except ValueError as exc:
                            raise RepositoryConflict(str(exc)) from exc
                        event = await self.filings.save_ready_event(event)
                    # Validate the saved input against strong canonical storage facts.
                    try:
                        current = FilingReady.from_artifacts(filing, children,
                            primary_document_name=progress.resolved_primary_document_name,
                            company_name=event.company_name, ready_at=event.ready_at)
                    except ValueError as exc:
                        raise RepositoryConflict(str(exc)) from exc
                    if current.snapshot_id != event.snapshot_id:
                        raise RepositoryConflict("stored filing disagrees with saved notification")
                    await self.publisher.publish_filing_ready(event)
                    at = self.control.now()
                    await self.control.checkpoint(lambda: self.filings.mark_ready_published(
                        accession_number, event.snapshot_id, at), operation="mark_ready_published", identity=accession_number)
                    if self.metrics is not None:
                        self.metrics.count("FilingsPublished")
                    LOGGER.info("Filing published", extra={"operation": "PUBLISH", "accession_number": accession_number,
                        "cik": filing.company_cik, "snapshot_id": event.snapshot_id})
                except Exception as exc:
                    if self.metrics is not None:
                        self.metrics.count("ArtifactPublishFailures")  # Existing alarm's metric name.
                    if not work_error(exc):
                        raise
                    durable = await self.filings.get_checkpoint(accession_number)
                    if durable.ready_published_at is not None or durable.terminal_at is not None:
                        continue
                    await self.control.failure(self.filings, accession_number, "PUBLISH", exc, durable)
                    durable = await self.filings.get_checkpoint(accession_number)
                    if durable.terminal_at is None:
                        await self.control.backoff(durable.failures("PUBLISH"))

    async def _ensure_membership(self, accession_number):
        """Adopt a legacy pending package only when its full selection is known.

        Current selection comes from the same package evidence already required
        to authorize legacy pending files. Never create newly recognized children.
        """
        progress = await self.filings.get_checkpoint(accession_number)
        if (progress is None or progress.enumeration_completed_at is None
                or progress.selected_artifact_ids is not None or progress.selection_blocked
                or progress.terminal_at is not None):
            return
        if self.discovery.selection is not None:
            decisions = self.discovery._decisions.get(accession_number)
            if decisions is None:
                return
            ids = sorted(accession_number + "/" + name for name, decision in decisions.items() if decision.selected)
        else:
            # Offline component replays without a selector use the original exact
            # enumeration count. In production, source-backed selection is required.
            ids, token = [], None
            while True:
                page = await self.artifacts.list_for_filing(accession_number, token=token)
                ids.extend(child.artifact_id for child in page.items)
                token = page.next_token
                if token is None:
                    break
            if len(ids) != progress.enumerated_artifact_count:
                return
            ids.sort()
        if not ids or accession_number + "/" + progress.resolved_primary_document_name not in ids:
            return
        for identity in ids:
            if await self.artifacts.get(identity) is None:
                return
        await self.filings.mark_enumerated(accession_number,
            primary_document_name=progress.resolved_primary_document_name,
            artifact_count=len(ids), completed_at=progress.enumeration_completed_at, artifact_ids=tuple(ids))

    async def _mark_children_published(self, children, at):
        # Compatibility for artifact readers: these files were included in an
        # acknowledged filing notification, never independently published.
        for child in children:
            if child.published_at is None:
                try:
                    await self.control.checkpoint(lambda child=child: self.artifacts.mark_published(child.artifact_id, at),
                        operation="mark_published", identity=child.artifact_id)
                except Exception:
                    durable = await self.artifacts.get(child.artifact_id)
                    if durable is None or durable.published_at is None:
                        raise
                if self.metrics is not None:
                    self.metrics.count("ArtifactsPublished")

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
