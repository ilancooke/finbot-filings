"""Fresh submissions and restart-safe package enumeration, without scheduling."""

from dataclasses import dataclass, replace
from collections import OrderedDict
import asyncio
import logging

from finbot_ingestion.selection.policy import (POLICY_VERSION, POLICY_UPGRADES, Decision,
    MAX_ROUTING_PARSE_BYTES, document_decision, primary_exhibits, primary_items)
from finbot_ingestion.selection.six_k import SIX_K_FORMS, select_six_k
from finbot_ingestion.sec.errors import SECDocumentTooLarge

from finbot_ingestion.repositories.errors import RepositoryNotFound
from finbot_ingestion.repositories.package_checkpoint import PackageCheckpoint
from .work_control import IdentityLocks, Outcome, work_error

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EnumerationResult:
    outcome: Outcome
    artifact_ids: tuple[str, ...] = ()


class DiscoveryService:
    def __init__(self, sec_client, sec_execution, filings, artifacts, control, *, locks=None, selection=None):
        self.sec, self.execution = sec_client, sec_execution
        self.filings, self.artifacts, self.control = filings, artifacts, control
        self.packages = PackageCheckpoint(filings, artifacts)
        self.locks = locks if locks is not None else IdentityLocks()
        self.metrics = None
        self.selection = selection
        self.downloader = None
        self.artifact_slots = asyncio.Semaphore(control.config.max_inflight_artifacts)
        # Metadata decisions only, never retained document bodies.
        self._decisions = OrderedDict()

    async def discover(self, company):
        if self.selection is not None:
            return tuple(o.filing.accession_number for o in await self.discover_with_evidence(company))
        # Legacy component replay; the production factory always supplies selection.
        observed = await self.execution.call(self.sec.get_company_submissions, company=company)
        for filing in observed:
            await self.control.checkpoint(lambda: self.filings.create_if_absent(filing),
                operation="create_filing", identity=filing.accession_number)
        return tuple(filing.accession_number for filing in observed)

    async def discover_with_evidence(self, company):
        from finbot_ingestion.domain.sec_items import FilingObservation
        from finbot_ingestion.repositories.package_checkpoint import assert_same_filing
        observed = await self.execution.call(self.sec.get_company_submissions_with_evidence, company=company)
        canonical = []
        for observation in observed:
            filing = observation.filing
            decision = None
            if self.selection is not None:
                evidence = observation.sec_items
                decision = self.selection.filing(filing, items=evidence.items, item_status=evidence.status)
                if decision.outcome == "EXCLUDE":
                    if decision.reason != "before_live_cutoff":
                        self._log(filing, decision)
                    continue
            created = await self.control.checkpoint(lambda: self.filings.create_if_absent(filing),
                operation="create_filing", identity=filing.accession_number)
            durable = await self.filings.get(filing.accession_number)
            if durable is None:
                raise RepositoryNotFound(filing.accession_number)
            assert_same_filing(durable, filing)
            if (durable.form_type, durable.filed_at) != (filing.form_type, filing.filed_at):
                raise ValueError("SEC evidence differs from canonical filing")
            if self.selection is not None:
                progress = await self.filings.get_checkpoint(filing.accession_number)
                if progress.selection_blocked or progress.terminal_at is not None:
                    continue
                if decision.selected and progress.selection_policy is None:
                    await self._record(self.filings, filing.accession_number, decision)
            canonical.append(FilingObservation(durable, observation.sec_items))
            if created and self.metrics is not None:
                self.metrics.count("FilingsDiscovered")
                self.metrics.latency("DiscoveryLatencyMs", durable.filed_at, durable.discovered_at)
        return tuple(canonical)

    async def enumerate_filing(self, accession_number):
        if self.selection is not None:
            # Routing can retain primary bytes through storage. Acquire the shared
            # slot before the filing lock, matching artifact dispatch lock order.
            async with self.artifact_slots:
                return await self._enumerate_filing(accession_number)
        return await self._enumerate_filing(accession_number)

    async def _enumerate_filing(self, accession_number):
        async with self.locks.hold("filing/" + accession_number):
            while True:
                filing = await self.filings.get(accession_number)
                progress = await self.filings.get_checkpoint(accession_number)
                if filing is None or progress is None:
                    raise RepositoryNotFound(accession_number)
                if progress.terminal_at is not None:
                    return EnumerationResult(Outcome.TERMINAL)
                if progress.selection_blocked:
                    return EnumerationResult(Outcome.SKIPPED)
                # Recovered legacy parents can be outside the cutoff even when
                # their original enumeration has already completed.
                if self.selection is not None:
                    scope = self.selection.filing(filing)
                    if scope.reason == "before_live_cutoff":
                        await self._record(self.filings, accession_number, scope)
                        return EnumerationResult(Outcome.SKIPPED)
                if progress.enumeration_completed_at is not None:
                    return EnumerationResult(Outcome.COMPLETE)
                try:
                    package = await self.execution.call(self.sec.get_filing_index, filing=filing)
                    decisions, primary_bytes = None, None
                    if self.selection is not None:
                        admission, decisions, primary_bytes = await self.select_package(filing, package, progress)
                        await self._record(self.filings, accession_number, admission)
                        if not admission.selected:
                            return EnumerationResult(Outcome.SKIPPED)
                        self._remember(filing, decisions)
                        for document in package.documents:
                            self._log(filing, decisions[document.filename], filename=document.filename)
                        package = replace(package, documents=tuple(d for d in package.documents if decisions[d.filename].selected))
                    children = package.artifacts(filing, discovered_at=self.control.now())
                    completed_at = self.control.now()
                    await self.control.checkpoint(lambda: self.packages.persist(filing, children,
                        primary_document_name=package.primary_document_name, completed_at=completed_at),
                        operation="persist_package", identity=accession_number)
                    if decisions is not None:
                        for child in children:
                            await self._record(self.artifacts, child.artifact_id, decisions[child.filename])
                        await self._reuse_primary(filing, package.primary_document_name, primary_bytes)
                    return EnumerationResult(Outcome.COMPLETE, tuple(child.artifact_id for child in children))
                except Exception as exc:
                    if not work_error(exc):
                        raise
                    durable = await self.filings.get_checkpoint(accession_number)
                    if durable is None:
                        raise RepositoryNotFound(accession_number) from exc
                    if durable.enumeration_completed_at is not None:
                        # A lost parent acknowledgment: children are recovered by indexes.
                        return EnumerationResult(Outcome.COMPLETE)
                    if durable.terminal_at is not None:
                        return EnumerationResult(Outcome.TERMINAL)
                    await self.control.failure(self.filings, accession_number, "ENUMERATE", exc, durable)
                    durable = await self.filings.get_checkpoint(accession_number)
                    if durable.terminal_at is not None:
                        return EnumerationResult(Outcome.TERMINAL)
                    await self.control.backoff(durable.failures("ENUMERATE"))

    def _log(self, filing, decision, *, filename=None):
        LOGGER.log(logging.WARNING if decision.outcome == "UNRESOLVED" else logging.INFO,
            "Acquisition selection", extra={"operation": "acquisition_selection",
                "cik": filing.company_cik, "accession_number": filing.accession_number,
                "artifact_id": filing.accession_number + "/" + filename if filename else None,
                "policy_version": POLICY_VERSION, "reason": decision.reason,
                "selection_decision": decision.outcome})

    async def _record(self, repo, identity, decision):
        at = self.control.now()
        await self.control.checkpoint(lambda: repo.record_selection(identity, decision, at,
            policy_version=POLICY_VERSION), operation="record_selection", identity=identity)

    async def _primary_bytes(self, filing, package, *, routing_only=False):
        from finbot_ingestion.sec.urls import document_url
        bound = self.downloader.max_artifact_bytes if self.downloader else 64 * 1024 * 1024
        if routing_only:
            bound = min(bound, MAX_ROUTING_PARSE_BYTES)
        return await self.execution.call(self.sec.download_document,
            url=document_url(filing.company_cik, filing.accession_number, package.primary_document_name),
            max_bytes=bound, context={"ticker": filing.ticker, "cik": filing.company_cik,
                "accession_number": filing.accession_number,
                "artifact_id": filing.accession_number + "/" + package.primary_document_name})

    async def select_package(self, filing, package, progress):
        """Evaluate a package without checkpoint writes (also used by operators)."""
        primary = None
        primary_document = next(d for d in package.documents if d.filename == package.primary_document_name)
        if not document_decision(filing, package, primary_document).selected:
            admission = Decision("UNRESOLVED", "unsupported_primary_document")
            self._log(filing, admission)
            return admission, {}, None
        if filing.form_type in SIX_K_FORMS:
            scope = self.selection.filing(filing)
            if scope.reason == "before_live_cutoff":
                return scope, {}, None
            if not package.primary_document_name.lower().endswith((".htm", ".html", ".txt", ".pdf")):
                return Decision("UNRESOLVED", "unsupported_primary_document"), {}, None
            admission, decisions = select_six_k(filing, package)
            if package.primary_document_name.lower().endswith((".htm", ".html", ".txt")):
                try:
                    primary = await self._primary_bytes(filing, package, routing_only=True)
                except SECDocumentTooLarge:
                    # Large primaries can still qualify from explicit index
                    # descriptions; unsupported inspection is not workflow failure.
                    primary = None
                if primary is not None:
                    admission, decisions = select_six_k(filing, package, primary.content)
            if not admission.selected:
                self._log(filing, admission)
            return admission, decisions, primary
        admission = self.selection.filing(filing, items=package.items, item_status=package.item_status)
        if (progress.selection_decision == "SELECT" and (progress.selection_policy == POLICY_VERSION
                or (progress.selection_policy, POLICY_VERSION) in POLICY_UPGRADES)):
            admission = Decision("SELECT", progress.selection_reason)
            if (filing.form_type in {"8-K", "8-K/A"} and package.item_status == "known"
                    and "2.02" not in package.items):
                # Conflicting affirmative submissions vs index evidence is visible,
                # and must not authorize any attachment downloads.
                return Decision("UNRESOLVED", "conflicting_items"), {}, None
        elif admission.outcome == "UNRESOLVED" and package.item_status == "absent":
            if package.primary_document_name.lower().endswith((".htm", ".html", ".txt")):
                primary = await self._primary_bytes(filing, package)
                items = primary_items(primary.content)
                admission = self.selection.filing(filing, items=items,
                    item_status="known" if items else "absent")
        if not admission.selected:
            self._log(filing, admission)
            return admission, {}, None
        decisions = {d.filename: document_decision(filing, package, d) for d in package.documents}
        if any(d.outcome == "UNRESOLVED" for d in decisions.values()) and package.primary_document_name.lower().endswith((".htm", ".html", ".txt")):
            if primary is None:
                primary = await self._primary_bytes(filing, package)
            references = primary_exhibits(filing, primary.content)
            decisions = {d.filename: document_decision(filing, package, d, references=references)
                         for d in package.documents}
        primary_decision = decisions[package.primary_document_name]
        if not primary_decision.selected:
            return Decision("UNRESOLVED", "unsupported_primary_document"), {}, None
        return admission, decisions, primary

    def _remember(self, filing, decisions):
        self._decisions[filing.accession_number] = decisions
        self._decisions.move_to_end(filing.accession_number)
        while len(self._decisions) > 32:
            self._decisions.popitem(last=False)

    async def _reuse_primary(self, filing, name, downloaded):
        if downloaded is None or self.downloader is None:
            return
        identity = filing.accession_number + "/" + name
        child = await self.artifacts.get(identity)
        progress = await self.artifacts.get_checkpoint(identity)
        if child is None or progress is None or child.stored_at is not None or progress.terminal_at is not None or progress.selection_blocked:
            return
        # The primary was already fetched to route exhibits. Preserve these exact
        # bytes through the same inspect/conditional-put path; do not keep a cache.
        stored = await self.downloader.acquire(child, filing, downloaded=downloaded)
        await self.control.checkpoint(lambda: self.artifacts.mark_stored(identity,
            stored.s3_uri, stored.stored_at, stored.content_type, stored.size_bytes),
            operation="mark_stored", identity=identity)
        if self.metrics is not None:
            self.metrics.count("ArtifactsStored")
            self.metrics.latency("DownloadLatencyMs", child.discovered_at, stored.stored_at)
            self.metrics.latency("IngestionLatencyMs", filing.filed_at, stored.stored_at)
        LOGGER.info("Routing primary stored", extra={"operation": "ACQUIRE", "artifact_id": identity})

    async def authorize_artifact(self, artifact, filing, parent, progress):
        """Check pending ACQUIRE and PUBLISH, including legacy direct dispatch."""
        if progress.selection_blocked or parent.selection_blocked:
            decision = Decision(parent.selection_decision, parent.selection_reason) if parent.selection_blocked else Decision(progress.selection_decision, progress.selection_reason)
            if not progress.selection_blocked:
                await self._record(self.artifacts, artifact.artifact_id, decision)
            return False
        if self.selection is None:
            return True
        scope = self.selection.filing(filing)
        if scope.reason == "before_live_cutoff":
            await self._record(self.artifacts, artifact.artifact_id, scope)
            self._log(filing, scope, filename=artifact.filename)
            return False
        if (progress.selection_policy == POLICY_VERSION and progress.selection_decision == "SELECT"
                and parent.selection_policy == POLICY_VERSION and parent.selection_decision == "SELECT"):
            return True
        async with self.locks.hold("filing/" + filing.accession_number):
            # Parent selection can have changed while waiting for enumeration.
            parent = await self.filings.get_checkpoint(filing.accession_number)
            if parent.selection_blocked:
                decision = Decision(parent.selection_decision, parent.selection_reason)
            else:
                decisions = self._decisions.get(filing.accession_number)
                if decisions is None:
                    package = await self.execution.call(self.sec.get_filing_index, filing=filing)
                    admission, decisions, primary = await self.select_package(filing, package, parent)
                    await self._record(self.filings, filing.accession_number, admission)
                    if not admission.selected:
                        await self._record(self.artifacts, artifact.artifact_id, admission)
                        return False
                    self._remember(filing, decisions)
                    # A legacy primary can already exist even while another child
                    # is being checked. Its conditional storage remains idempotent.
                    primary_id = filing.accession_number + "/" + package.primary_document_name
                    existing = await self.artifacts.get(primary_id)
                    if existing is not None:
                        await self._record(self.artifacts, primary_id, decisions[package.primary_document_name])
                        await self._reuse_primary(filing, package.primary_document_name, primary)
                decision = decisions.get(artifact.filename, Decision("UNRESOLVED", "document_not_in_snapshot"))
            await self._record(self.artifacts, artifact.artifact_id, decision)
            self._log(filing, decision, filename=artifact.filename)
            return decision.selected
