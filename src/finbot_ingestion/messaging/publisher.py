from typing import Protocol

from finbot_ingestion.domain.events import ArtifactReady


class ArtifactEventPublisher(Protocol):
    async def publish_artifact_ready(self, event: ArtifactReady) -> None: ...


class InvalidPublishResponse(RuntimeError):
    """No valid acknowledgment; the same logical message remains retryable."""


def validate_message_id(response):
    value = response.get("MessageId") if isinstance(response, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise InvalidPublishResponse("publication returned no message ID")


def bounded_json_message(text):
    # Stay within the default SNS limit and below the SQS maximum.
    if len(text.encode("utf-8")) > 256 * 1024:
        raise ValueError("metadata message exceeds 256 KiB")
    return text
