from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from finbot_ingestion.domain import Artifact
from finbot_ingestion.domain.validation import utc_datetime, validate_s3_uri
from finbot_ingestion.sec.client import DownloadedDocument


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    s3_uri: str
    stored_at: datetime
    content_type: str | None
    size_bytes: int

    def __post_init__(self):
        validate_s3_uri(self.s3_uri)
        object.__setattr__(self, "stored_at", utc_datetime(self.stored_at, "stored_at"))
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("size_bytes must be a nonnegative integer")
        if self.content_type is not None and (not isinstance(self.content_type, str) or not self.content_type):
            raise ValueError("content_type must be nonempty text")


class ArtifactStore(Protocol):
    async def inspect(self, artifact: Artifact) -> StoredArtifact | None: ...
    async def put_if_absent(self, artifact: Artifact, downloaded: DownloadedDocument) -> StoredArtifact: ...
