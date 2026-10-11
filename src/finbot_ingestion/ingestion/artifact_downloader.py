"""Inspect before downloading and preserve original response-content bytes."""

import logging

from finbot_ingestion.selection.policy import POLICY_VERSION, image_only_html

LOGGER = logging.getLogger(__name__)


class ArtifactDownloader:
    def __init__(self, sec_client, sec_execution, store, *, max_artifact_bytes):
        if type(max_artifact_bytes) is not int or not 1 <= max_artifact_bytes <= 64 * 1024 * 1024:
            raise ValueError("max_artifact_bytes must be in [1, 67108864]")
        self.sec, self.execution, self.store = sec_client, sec_execution, store
        self.max_artifact_bytes = max_artifact_bytes

    async def acquire(self, artifact, filing, *, downloaded=None):
        stored = await self.store.inspect(artifact, filing)
        if stored is not None:
            return stored
        if downloaded is None:
            downloaded = await self.execution.call(self.sec.download_document, url=artifact.sec_url,
                                                   max_bytes=self.max_artifact_bytes)
        elif downloaded.source_url != artifact.sec_url or downloaded.size_bytes > self.max_artifact_bytes:
            raise ValueError("routing bytes disagree with the artifact URL or size bound")
        if (artifact.filename.lower().endswith((".htm", ".html"))
                and image_only_html(downloaded.content)):
            LOGGER.warning("Selected HTML has little text and relies on excluded images", extra={
                "operation": "acquisition_selection", "artifact_id": artifact.artifact_id,
                "policy_version": POLICY_VERSION, "reason": "possible_image_only_content"})
        return await self.store.put_if_absent(artifact, filing, downloaded)
