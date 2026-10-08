"""A durable terminal observation produces one stable logical failure envelope."""

from dataclasses import dataclass
import json
from typing import Protocol

from finbot_ingestion.domain import Artifact, Filing
from finbot_ingestion.domain.checkpoints import ArtifactCheckpoint, FilingCheckpoint
from finbot_ingestion.domain.validation import utc_text
from .publisher import bounded_json_message, validate_message_id


@dataclass(frozen=True, slots=True)
class WorkFailure:
    """JSON is immutable and derived solely from durable source/checkpoint facts."""
    failure_id: str
    message: str

    @classmethod
    def from_checkpoint(cls, source, progress):
        if progress.terminal_at is None:
            raise ValueError("failure envelope requires terminal facts")
        if isinstance(source, Filing) and isinstance(progress, FilingCheckpoint):
            kind, identity = "filing", source.accession_number
            if progress.accession_number != identity:
                raise ValueError("filing checkpoint identity mismatch")
        elif isinstance(source, Artifact) and isinstance(progress, ArtifactCheckpoint):
            kind, identity = "artifact", source.artifact_id
            if progress.artifact_id != identity:
                raise ValueError("artifact checkpoint identity mismatch")
        else:
            raise ValueError("matching source/checkpoint types required")
        failure_id = f"{kind}/{identity}/{progress.terminal_stage}/{utc_text(progress.terminal_at)}"
        message = json.dumps({"event_type": "ingestion.work_failed", "schema_version": "1.0",
            "failure_id": failure_id, "work_type": kind, "work_id": identity,
            "stage": progress.terminal_stage, "terminal_at": utc_text(progress.terminal_at),
            "error_type": progress.terminal_error_type, "error": progress.terminal_error,
            "failed_attempts": progress.failures(progress.terminal_stage),
            "cik": source.company_cik, "ticker": source.ticker, "form_type": source.form_type,
            "accession_number": source.accession_number,
            "source_url": source.filing_index_url if kind == "filing" else source.sec_url,
            "s3_uri": None if kind == "filing" else source.s3_uri},
            ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return cls(failure_id, bounded_json_message(message))


class DeadLetterPublisher(Protocol):
    async def send(self, failure: WorkFailure) -> None: ...


class SQSDeadLetterPublisher:
    def __init__(self, execution, config):
        if execution.config.region != config.region:
            raise ValueError("SQS client and queue regions must match")
        self.execution, self.config = execution, config

    async def send(self, failure):
        response = await self.execution.call(self.execution.client.send_message,
            QueueUrl=self.config.dead_letter_queue_url, MessageBody=bounded_json_message(failure.message))
        validate_message_id(response)
