"""Versioned ArtifactReady wire contract; publication itself is a later phase."""

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .artifact import Artifact
from .filing import Filing
from .identity import (
    artifact_identity,
    normalize_accession_number,
    normalize_cik,
    normalize_form,
    normalize_ticker,
)
from .validation import utc_datetime, utc_text, validate_s3_uri


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactReady:
    artifact_id: str
    filing_id: str
    cik: str
    ticker: str
    form_type: str
    document_type: str | None
    filename: str
    s3_uri: str
    filed_at: datetime
    discovered_at: datetime
    stored_at: datetime
    event_type: str = field(default="artifact.ready", init=False)
    schema_version: str = field(default="1.0", init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "filing_id", normalize_accession_number(self.filing_id))
        object.__setattr__(self, "cik", normalize_cik(self.cik))
        object.__setattr__(self, "ticker", normalize_ticker(self.ticker))
        object.__setattr__(self, "form_type", normalize_form(self.form_type))
        if self.artifact_id != artifact_identity(self.filing_id, self.filename):
            raise ValueError("artifact_id does not match accession and filename")
        validate_s3_uri(self.s3_uri)
        for name in ("filed_at", "discovered_at", "stored_at"):
            object.__setattr__(self, name, utc_datetime(getattr(self, name), name))

    @classmethod
    def from_artifact(cls, filing: Filing, artifact: Artifact) -> "ArtifactReady":
        for name in ("accession_number", "company_cik", "ticker", "form_type"):
            if getattr(filing, name) != getattr(artifact, name):
                raise ValueError(f"filing and artifact disagree on {name}")
        if artifact.s3_uri is None or artifact.stored_at is None:
            raise ValueError("ArtifactReady requires a stored artifact")
        return cls(
            artifact_id=artifact.artifact_id, filing_id=filing.accession_number,
            cik=artifact.company_cik, ticker=artifact.ticker, form_type=artifact.form_type,
            document_type=artifact.document_type, filename=artifact.filename,
            s3_uri=artifact.s3_uri, filed_at=filing.filed_at,
            discovered_at=artifact.discovered_at, stored_at=artifact.stored_at,
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for name in ("filed_at", "discovered_at", "stored_at"):
            value[name] = utc_text(getattr(self, name))
        return value

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
