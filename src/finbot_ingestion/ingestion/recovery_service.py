"""Explicit bounded-page recovery passes; continuous cadence belongs to runtime."""

from dataclasses import dataclass
import logging

from .work_control import Outcome, work_error

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RecoverySummary:
    candidates: int = 0
    complete: int = 0
    terminal: int = 0
    deferred: int = 0
    errors: int = 0


class RecoveryService:
    def __init__(self, worker, *, page_size=None):
        self.worker = worker
        self.progress = None
        self.page_size = worker.control.config.recovery_page_size if page_size is None else page_size
        if type(self.page_size) is not int or not 1 <= self.page_size <= 1000:
            raise ValueError("page_size must be in [1, 1000]")

    async def _pages(self, fetch, action, summary):
        token = None
        while True:
            page = await fetch(token)
            for source in page.items:
                if self.progress is not None:
                    self.progress()
                summary.candidates += 1
                try:
                    outcome = await action(source)
                    if outcome == Outcome.COMPLETE:
                        summary.complete += 1
                    elif outcome == Outcome.TERMINAL:
                        summary.terminal += 1
                    else:
                        summary.deferred += 1
                except Exception as exc:
                    # Infrastructure/config/programming errors stay visible to the caller.
                    if not work_error(exc):
                        raise
                    summary.errors += 1
                    LOGGER.warning("Recovery candidate failed", extra={"operation": "recovery",
                        "error_type": type(exc).__name__, "accession_number": source.accession_number,
                        "artifact_id": getattr(source, "artifact_id", None)})
            token = page.next_token
            if token is None:
                return

    async def run_pass(self):
        summary = RecoverySummary()
        w = self.worker
        await self._pages(lambda token: w.filings.list_pending(page_size=self.page_size, token=token),
                          lambda filing: w.process_filing(filing.accession_number), summary)
        for kind in ("ACQUIRE", "PUBLISH"):
            await self._pages(lambda token: w.artifacts.list_pending(kind, page_size=self.page_size, token=token),
                              lambda artifact: w.process_artifact(artifact.artifact_id), summary)
        await self._pages(lambda token: w.filings.list_pending(kind="DEAD_LETTER", page_size=self.page_size, token=token),
                          lambda filing: w.send_dead_letter("filing", filing.accession_number), summary)
        await self._pages(lambda token: w.artifacts.list_pending("DEAD_LETTER", page_size=self.page_size, token=token),
                          lambda artifact: w.send_dead_letter("artifact", artifact.artifact_id), summary)
        return summary
