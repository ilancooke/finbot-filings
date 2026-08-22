"""Immutable-by-default local storage for original SEC filing documents."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from finbot_filings.layout import filing_directory
from finbot_filings.models import Filing


@dataclass(frozen=True, slots=True)
class FilingPaths:
    directory: Path
    document: Path
    metadata: Path


@dataclass(frozen=True, slots=True)
class StoreResult:
    paths: FilingPaths
    written: bool


class LocalFilingStorage:
    def __init__(
        self,
        download_folder: Path,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.download_folder = Path(download_folder)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def paths_for(self, filing: Filing) -> FilingPaths:
        directory = filing_directory(
            self.download_folder,
            ticker=filing.ticker,
            form=filing.form,
            accession_number=filing.accession_number,
        )
        return FilingPaths(
            directory=directory,
            document=directory / "filing.html",
            metadata=directory / "metadata.json",
        )

    def exists(self, filing: Filing) -> bool:
        return self.paths_for(filing).document.exists()

    def is_complete(self, filing: Filing) -> bool:
        paths = self.paths_for(filing)
        return paths.document.exists() and paths.metadata.exists()

    def store(
        self,
        filing: Filing,
        document_bytes: bytes,
        *,
        overwrite: bool = False,
        metadata_updates: Mapping[str, Any] | None = None,
    ) -> StoreResult:
        paths = self.paths_for(filing)
        if paths.document.exists() and paths.metadata.exists() and not overwrite:
            return StoreResult(paths=paths, written=False)

        paths.directory.mkdir(parents=True, exist_ok=True)
        metadata = filing.to_dict()
        if metadata_updates:
            metadata.update(metadata_updates)
        metadata["downloaded_at"] = self._now().astimezone(timezone.utc).isoformat()

        document_temp = paths.directory / ".filing.html.tmp"
        metadata_temp = paths.directory / ".metadata.json.tmp"
        document_temp.write_bytes(document_bytes)
        metadata_temp.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(document_temp, paths.document)
        os.replace(metadata_temp, paths.metadata)
        return StoreResult(paths=paths, written=True)
