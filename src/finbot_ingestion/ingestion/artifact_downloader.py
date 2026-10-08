"""Inspect before downloading and preserve original response-content bytes."""


class ArtifactDownloader:
    def __init__(self, sec_client, sec_execution, store, *, max_artifact_bytes):
        if type(max_artifact_bytes) is not int or not 1 <= max_artifact_bytes <= 64 * 1024 * 1024:
            raise ValueError("max_artifact_bytes must be in [1, 67108864]")
        self.sec, self.execution, self.store = sec_client, sec_execution, store
        self.max_artifact_bytes = max_artifact_bytes

    async def acquire(self, artifact):
        stored = await self.store.inspect(artifact)
        if stored is not None:
            return stored
        downloaded = await self.execution.call(self.sec.download_document, url=artifact.sec_url,
                                               max_bytes=self.max_artifact_bytes)
        return await self.store.put_if_absent(artifact, downloaded)
