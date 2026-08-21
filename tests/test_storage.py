from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from finbot_filings.models import Filing
from finbot_filings.storage.local import LocalFilingStorage


def test_local_path_construction(tmp_path: Path, filing: Filing) -> None:
    paths = LocalFilingStorage(tmp_path).paths_for(filing)
    expected = tmp_path / "AAPL" / "0000320193-25-000079"
    assert paths.directory == expected
    assert paths.document == expected / "filing.html"
    assert paths.metadata == expected / "metadata.json"


def test_metadata_serialization_and_exact_document_bytes(
    tmp_path: Path, filing: Filing
) -> None:
    now = datetime(2026, 8, 20, 12, 30, tzinfo=timezone.utc)
    storage = LocalFilingStorage(tmp_path, now=lambda: now)
    source = b"\x00<html>SEC\r\nsource</html>\xff"
    result = storage.store(filing, source)
    metadata = json.loads(result.paths.metadata.read_text(encoding="utf-8"))

    assert result.written is True
    assert result.paths.document.read_bytes() == source
    assert metadata["ticker"] == "AAPL"
    assert metadata["cik"] == 320193
    assert metadata["filing_date"] == "2025-10-31"
    assert metadata["report_date"] == "2025-09-27"
    assert metadata["document_url"] == filing.document_url
    assert metadata["downloaded_at"] == "2026-08-20T12:30:00+00:00"


def test_existing_file_is_skipped_by_default(tmp_path: Path, filing: Filing) -> None:
    storage = LocalFilingStorage(tmp_path)
    first = storage.store(filing, b"original")
    original_metadata = first.paths.metadata.read_bytes()
    second = storage.store(filing, b"replacement")
    assert second.written is False
    assert second.paths.document.read_bytes() == b"original"
    assert second.paths.metadata.read_bytes() == original_metadata


def test_overwrite_replaces_document_and_metadata(tmp_path: Path, filing: Filing) -> None:
    moments = iter(
        [
            datetime(2026, 8, 20, tzinfo=timezone.utc),
            datetime(2026, 8, 21, tzinfo=timezone.utc),
        ]
    )
    storage = LocalFilingStorage(tmp_path, now=lambda: next(moments))
    first = storage.store(filing, b"original")
    first_metadata = first.paths.metadata.read_bytes()
    second = storage.store(filing, b"replacement", overwrite=True)
    assert second.written is True
    assert second.paths.document.read_bytes() == b"replacement"
    assert second.paths.metadata.read_bytes() != first_metadata
