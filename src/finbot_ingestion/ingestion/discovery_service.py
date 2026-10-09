"""Fresh submissions and restart-safe package enumeration, without scheduling."""

from dataclasses import dataclass

from finbot_ingestion.repositories.errors import RepositoryNotFound
from finbot_ingestion.repositories.package_checkpoint import PackageCheckpoint
from .work_control import IdentityLocks, Outcome, work_error


@dataclass(frozen=True, slots=True)
class EnumerationResult:
    outcome: Outcome
    artifact_ids: tuple[str, ...] = ()


class DiscoveryService:
    def __init__(self, sec_client, sec_execution, filings, artifacts, control, *, locks=None):
        self.sec, self.execution = sec_client, sec_execution
        self.filings, self.artifacts, self.control = filings, artifacts, control
        self.packages = PackageCheckpoint(filings, artifacts)
        self.locks = locks if locks is not None else IdentityLocks()
        self.metrics = None

    async def discover(self, company):
        # Fetch/parse errors are surfaced; no response watermark or synthetic filing.
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
            created = await self.control.checkpoint(lambda: self.filings.create_if_absent(filing),
                operation="create_filing", identity=filing.accession_number)
            durable = await self.filings.get(filing.accession_number)
            if durable is None:
                raise RepositoryNotFound(filing.accession_number)
            assert_same_filing(durable, filing)
            if (durable.form_type, durable.filed_at) != (filing.form_type, filing.filed_at):
                raise ValueError("SEC evidence differs from canonical filing")
            canonical.append(FilingObservation(durable, observation.sec_items))
            if created and self.metrics is not None:
                self.metrics.count("FilingsDiscovered")
                self.metrics.latency("DiscoveryLatencyMs", durable.filed_at, durable.discovered_at)
        return tuple(canonical)

    async def enumerate_filing(self, accession_number):
        async with self.locks.hold("filing/" + accession_number):
            while True:
                filing = await self.filings.get(accession_number)
                progress = await self.filings.get_checkpoint(accession_number)
                if filing is None or progress is None:
                    raise RepositoryNotFound(accession_number)
                if progress.terminal_at is not None:
                    return EnumerationResult(Outcome.TERMINAL)
                if progress.enumeration_completed_at is not None:
                    return EnumerationResult(Outcome.COMPLETE)
                try:
                    package = await self.execution.call(self.sec.get_filing_index, filing=filing)
                    children = package.artifacts(filing, discovered_at=self.control.now())
                    completed_at = self.control.now()
                    await self.control.checkpoint(lambda: self.packages.persist(filing, children,
                        primary_document_name=package.primary_document_name, completed_at=completed_at),
                        operation="persist_package", identity=accession_number)
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
