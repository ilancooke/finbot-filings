"""Immutable filing-level notification and deterministic input identity."""

from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import json

from .identity import artifact_identity, normalize_accession_number, normalize_cik, normalize_form, normalize_ticker
from .validation import utc_datetime, validate_s3_uri


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def timestamp(value):
    return utc_datetime(value, "timestamp").isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadyArtifact:
    artifact_id: str
    filename: str
    s3_uri: str
    size_bytes: int
    document_type: str | None = None
    content_type: str | None = None

    def __post_init__(self):
        if not isinstance(self.artifact_id, str):
            raise ValueError("artifact_id must be a string")
        accession, separator, filename = self.artifact_id.partition("/")
        if not separator or filename != self.filename or artifact_identity(accession, filename) != self.artifact_id:
            raise ValueError("artifact_id must agree with accession and filename")
        validate_s3_uri(self.s3_uri)
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("size_bytes must be a nonnegative integer")
        for name in ("document_type", "content_type"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be omitted or nonempty")

    def to_dict(self):
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass(frozen=True, slots=True, kw_only=True)
class FilingReady:
    filing_id: str
    cik: str
    ticker: str
    company_name: str
    form_type: str
    filed_at: datetime
    ready_at: datetime
    primary_artifact_id: str
    artifacts: tuple[ReadyArtifact, ...]
    snapshot_id: str = field(init=False)
    event_type: str = field(default="filing.ready", init=False)
    schema_version: str = field(default="1.0", init=False)

    def __post_init__(self):
        for name, normalize in (("filing_id", normalize_accession_number), ("cik", normalize_cik),
                                ("ticker", normalize_ticker), ("form_type", normalize_form)):
            object.__setattr__(self, name, normalize(getattr(self, name)))
        if not isinstance(self.company_name, str) or not self.company_name.strip():
            raise ValueError("company_name must be nonempty")
        for name in ("filed_at", "ready_at"):
            object.__setattr__(self, name, utc_datetime(getattr(self, name), name))
        if not all(isinstance(child, ReadyArtifact) for child in self.artifacts):
            raise ValueError("artifacts must contain ReadyArtifact entries")
        children = tuple(sorted(self.artifacts, key=lambda child: child.artifact_id))
        ids = [child.artifact_id for child in children]
        if not ids or len(set(ids)) != len(ids) or ids.count(self.primary_artifact_id) != 1:
            raise ValueError("manifest must be nonempty, unique and contain exactly one primary")
        if any(not identity.startswith(self.filing_id + "/") for identity in ids):
            raise ValueError("manifest contains a different filing")
        object.__setattr__(self, "artifacts", children)
        object.__setattr__(self, "snapshot_id", hashlib.sha256(canonical_json(self.manifest()).encode("utf-8")).hexdigest())

    def manifest(self):
        return dict(filing_id=self.filing_id, cik=self.cik, ticker=self.ticker, company_name=self.company_name,
                    form_type=self.form_type, filed_at=timestamp(self.filed_at),
                    primary_artifact_id=self.primary_artifact_id, artifacts=[child.to_dict() for child in self.artifacts])

    def to_dict(self):
        return {**self.manifest(), "event_type": self.event_type, "schema_version": self.schema_version,
                "snapshot_id": self.snapshot_id, "ready_at": timestamp(self.ready_at)}

    def to_json(self):
        return canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, message):
        value = json.loads(message)
        if value.pop("event_type") != "filing.ready" or value.pop("schema_version") != "1.0":
            raise ValueError("unsupported filing notification")
        snapshot_id = value.pop("snapshot_id")
        for name in ("filed_at", "ready_at"):
            value[name] = datetime.fromisoformat(value[name])
        value["artifacts"] = tuple(ReadyArtifact(**child) for child in value["artifacts"])
        event = cls(**value)
        if event.snapshot_id != snapshot_id or event.to_json() != message:
            raise ValueError("filing notification hash or canonical encoding mismatch")
        return event

    @classmethod
    def from_artifacts(cls, filing, artifacts, *, primary_document_name, company_name, ready_at):
        children = []
        for artifact in artifacts:
            for name in ("accession_number", "company_cik", "ticker", "form_type"):
                if getattr(filing, name) != getattr(artifact, name):
                    raise ValueError(f"filing and artifact disagree on {name}")
            if artifact.stored_at is None or artifact.s3_uri is None or artifact.size_bytes is None:
                raise ValueError("FilingReady requires every artifact stored with a byte count")
            children.append(ReadyArtifact(artifact_id=artifact.artifact_id, filename=artifact.filename,
                s3_uri=artifact.s3_uri, size_bytes=artifact.size_bytes,
                document_type=artifact.document_type, content_type=artifact.content_type))
        return cls(filing_id=filing.accession_number, cik=filing.company_cik, ticker=filing.ticker,
            company_name=company_name, form_type=filing.form_type, filed_at=filing.filed_at,
            ready_at=ready_at, primary_artifact_id=artifact_identity(filing.accession_number, primary_document_name),
            artifacts=tuple(children))
