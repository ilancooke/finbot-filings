"""Conditional S3 writes with inspectable, immutable first provenance."""

import json
from urllib.parse import quote

from botocore.exceptions import ClientError, ConnectionClosedError, EndpointConnectionError, HTTPClientError

from finbot_ingestion.domain.identity import artifact_key
from finbot_ingestion.domain.validation import utc_text
from .artifact_store import StoredArtifact
from .errors import StorageConflict, StorageLimitError, StorageNotVisibleError


def provenance(artifact):
    return {"version": 1, "artifact_id": artifact.artifact_id, "cik": artifact.company_cik,
            "ticker": artifact.ticker, "form_type": artifact.form_type,
            "document_type": artifact.document_type, "sec_url": artifact.sec_url,
            "discovered_at": utc_text(artifact.discovered_at)}


class S3ArtifactStore:
    def __init__(self, execution, config):
        self.execution, self.config = execution, config

    def _key(self, artifact):
        key = artifact_key(artifact.company_cik, artifact.accession_number, artifact.filename)
        if len(key.encode("utf-8")) > 1024:
            raise StorageLimitError("S3 key exceeds 1024 UTF-8 bytes")
        return key

    async def _head(self, key):
        try:
            return await self.execution.call(self.execution.client.head_object,
                                             Bucket=self.config.bucket, Key=key)
        except ClientError as exc:
            # HEAD returns generic errors; permission-dependent 403 is never absence.
            if (exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404
                    and exc.response.get("Error", {}).get("Code") in ("404", "NotFound", "NoSuchKey")):
                return None
            raise

    async def inspect(self, artifact):
        key = self._key(artifact)
        head = await self._head(key)
        if head is None:
            return None
        try:
            metadata = head["Metadata"]
            raw = metadata["finbot-provenance"]
            if not isinstance(raw, str) or len(raw.encode("ascii")) > 1800:
                raise ValueError("invalid provenance header")
            observed = json.loads(raw)
            if not isinstance(observed, dict) or type(observed.get("version")) is not int or observed != provenance(artifact):
                raise ValueError("source provenance mismatch")
            content_type = json.loads(metadata["finbot-content-type"])
            if content_type is not None and head.get("ContentType") != content_type:
                raise ValueError("content type disagrees with provenance")
            size = head["ContentLength"]
            if type(size) is not int or not 0 <= size <= self.config.max_artifact_bytes:
                raise ValueError("invalid or oversized object length")
            return StoredArtifact(f"s3://{self.config.bucket}/{quote(key, safe='/')}", head["LastModified"], content_type, size)
        except (KeyError, ValueError, TypeError, UnicodeError) as exc:
            raise StorageConflict("existing S3 object lacks compatible, valid provenance") from exc

    async def put_if_absent(self, artifact, downloaded):
        key = self._key(artifact)
        if not isinstance(downloaded.content, bytes) or len(downloaded.content) > self.config.max_artifact_bytes:
            raise StorageLimitError("artifact exceeds MAX_ARTIFACT_BYTES or is not bytes")
        # Redirected official SEC source is allowed; persist canonical original URL.
        from finbot_ingestion.sec.client import SecClient
        SecClient._validate_url(downloaded.source_url)
        content_type = downloaded.content_type
        if content_type is not None and (not isinstance(content_type, str) or not content_type
                or any(ord(c) < 32 or ord(c) > 126 for c in content_type)):
            raise StorageLimitError("content type is not a safe HTTP header")
        metadata = {"finbot-provenance": json.dumps(provenance(artifact), ensure_ascii=True, sort_keys=True),
                    "finbot-content-type": json.dumps(content_type, ensure_ascii=True)}
        if len(metadata["finbot-provenance"].encode("ascii")) > 1800 or sum(
                len(k.encode("ascii")) + len(v.encode("ascii")) for k, v in metadata.items()) > 2048:
            raise StorageLimitError("artifact provenance exceeds S3 metadata limit")
        params = dict(Bucket=self.config.bucket, Key=key, Body=downloaded.content,
                      Metadata=metadata, IfNoneMatch="*")
        if content_type is not None:
            params["ContentType"] = content_type
        try:
            await self.execution.call(self.execution.client.put_object, **params)
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") not in (409, 412):
                # A server error can also be an ambiguous success. Inspect before retry.
                if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) < 500:
                    raise
                existing = await self.inspect(artifact)
                if existing is not None:
                    return existing
                raise
            existing = await self.inspect(artifact)
            if existing is None:
                if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 409:
                    raise
                raise StorageNotVisibleError("conflicting object is not inspectable") from exc
            return existing
        except (HTTPClientError, EndpointConnectionError, ConnectionClosedError):
            existing = await self.inspect(artifact)
            if existing is not None:
                return existing
            raise
        stored = await self.inspect(artifact)
        if stored is None:
            raise StorageNotVisibleError("successful S3 write is not inspectable")
        if stored.size_bytes != len(downloaded.content) or stored.content_type != content_type:
            raise StorageConflict("newly stored object metadata disagrees with download")
        return stored
