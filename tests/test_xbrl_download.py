from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from finbot_filings.sec.client import SECDataError
from finbot_filings.xbrl.download import (
    XBRLPackageLocation,
    accession_directory_url,
    accession_index_json_url,
    discover_xbrl_package,
    download_xbrl_packages,
    store_xbrl_package,
)


ACCESSION = "0000320193-26-000020"
INDEX_URL = (
    "https://www.sec.gov/Archives/edgar/data/320193/"
    "000032019326000020/index.json"
)
PACKAGE_NAME = "0000320193-26-000020-xbrl.zip"
PACKAGE_URL = INDEX_URL.removesuffix("index.json") + PACKAGE_NAME
INSTANCE_NAME = "aapl-20251227_htm.xml"
INSTANCE_URL = INDEX_URL.removesuffix("index.json") + INSTANCE_NAME


class FakeClient:
    def __init__(
        self,
        *,
        payload: Any,
        package_bytes: bytes = b"",
        instance_bytes: bytes = b"",
    ) -> None:
        self.payload = payload
        self.package_bytes = package_bytes
        self.instance_bytes = instance_bytes
        self.json_calls: list[str] = []
        self.byte_calls: list[str] = []

    def get_json(self, url: str) -> Any:
        self.json_calls.append(url)
        return self.payload

    def get_bytes(self, url: str) -> bytes:
        self.byte_calls.append(url)
        return self.instance_bytes if url == INSTANCE_URL else self.package_bytes


def _zip_bytes(files: dict[str, bytes] | None = None) -> bytes:
    destination = io.BytesIO()
    with zipfile.ZipFile(destination, "w") as archive:
        for name, value in (files or {"aapl-20251227.xsd": b"<schema/>"}).items():
            archive.writestr(name, value)
    return destination.getvalue()


def _instance_bytes() -> bytes:
    return b'<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"/>'


def _location() -> XBRLPackageLocation:
    return XBRLPackageLocation(
        INDEX_URL, PACKAGE_URL, PACKAGE_NAME, INSTANCE_URL, INSTANCE_NAME
    )


def _index_payload(*names: str) -> dict[str, object]:
    return {"directory": {"item": [{"name": name} for name in names]}}


def _filing_metadata() -> dict[str, object]:
    return {
        "ticker": "AAPL",
        "cik": 320193,
        "form": "10-Q",
        "accession_number": ACCESSION,
    }


def _write_filing_metadata(root: Path) -> Path:
    filing_directory = root / "AAPL" / "10-Q" / ACCESSION
    filing_directory.mkdir(parents=True)
    (filing_directory / "metadata.json").write_text(
        json.dumps(_filing_metadata()), encoding="utf-8"
    )
    return filing_directory


def test_accession_urls_use_numeric_cik_and_dashless_accession() -> None:
    assert accession_directory_url(320193, ACCESSION) == INDEX_URL.removesuffix(
        "/index.json"
    )
    assert accession_index_json_url(320193, ACCESSION) == INDEX_URL


def test_discover_xbrl_package_selects_sec_generated_zip() -> None:
    client = FakeClient(
        payload=_index_payload(
            "filing.htm", PACKAGE_NAME, INSTANCE_NAME, "Financial_Report.xlsx"
        )
    )

    location = discover_xbrl_package(client, cik=320193, accession_number=ACCESSION)

    assert location == _location()
    assert client.json_calls == [INDEX_URL]


def test_discover_xbrl_package_returns_none_when_not_listed() -> None:
    client = FakeClient(payload=_index_payload("filing.htm", "report.xml"))

    assert (
        discover_xbrl_package(client, cik=320193, accession_number=ACCESSION)
        is None
    )


@pytest.mark.parametrize(
    "payload, message",
    [
        ({}, "no directory object"),
        (_index_payload("../unsafe-xbrl.zip"), "unsafe XBRL filename"),
        (
            _index_payload("first-xbrl.zip", "second-xbrl.zip", INSTANCE_NAME),
            "multiple XBRL packages",
        ),
        (_index_payload(PACKAGE_NAME), "exactly one generated XBRL instance"),
    ],
)
def test_discover_xbrl_package_rejects_invalid_indexes(
    payload: object, message: str
) -> None:
    with pytest.raises(SECDataError, match=message):
        discover_xbrl_package(
            FakeClient(payload=payload), cik=320193, accession_number=ACCESSION
        )


def test_store_xbrl_package_preserves_zip_and_writes_provenance(tmp_path: Path) -> None:
    package_bytes = _zip_bytes({"aapl-20251227.xsd": b"<schema/>"})
    instance_bytes = _instance_bytes()

    paths = store_xbrl_package(
        filing_directory=tmp_path,
        filing_metadata=_filing_metadata(),
        location=_location(),
        package_bytes=package_bytes,
        instance_bytes=instance_bytes,
        now=lambda: datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
    )

    assert paths.package.read_bytes() == package_bytes
    assert paths.instance.read_bytes() == instance_bytes
    metadata = json.loads(paths.metadata.read_text(encoding="utf-8"))
    assert metadata["source_package_url"] == PACKAGE_URL
    assert metadata["sha256"] == hashlib.sha256(package_bytes).hexdigest()
    assert metadata["byte_count"] == len(package_bytes)
    assert metadata["member_count"] == 1
    assert metadata["package_instance_document_candidates"] == []
    assert metadata["instance_source_url"] == INSTANCE_URL
    assert metadata["instance_sha256"] == hashlib.sha256(instance_bytes).hexdigest()
    assert metadata["downloaded_at"] == "2026-08-22T12:00:00+00:00"


def test_store_xbrl_package_rejects_invalid_zip_without_writing(tmp_path: Path) -> None:
    with pytest.raises(SECDataError, match="invalid XBRL ZIP"):
        store_xbrl_package(
            filing_directory=tmp_path,
            filing_metadata=_filing_metadata(),
            location=_location(),
            package_bytes=b"not a zip",
            instance_bytes=_instance_bytes(),
        )

    assert not (tmp_path / "xbrl" / "package.zip").exists()


def test_download_xbrl_packages_downloads_then_skips_complete_package(
    tmp_path: Path,
) -> None:
    filing_directory = _write_filing_metadata(tmp_path)
    client = FakeClient(
        payload=_index_payload(PACKAGE_NAME, INSTANCE_NAME),
        package_bytes=_zip_bytes(),
        instance_bytes=_instance_bytes(),
    )
    output: list[str] = []

    first = download_xbrl_packages(
        client=client, download_root=tmp_path, form_type="10-q", printer=output.append
    )
    second = download_xbrl_packages(
        client=client, download_root=tmp_path, form_type="10-Q", printer=output.append
    )

    assert first.files_found == 1
    assert first.processed == 1
    assert first.downloaded == 1
    assert first.failed == 0
    assert second.files_found == 1
    assert second.processed == 0
    assert second.skipped == 1
    assert client.json_calls == [INDEX_URL]
    assert client.byte_calls == [PACKAGE_URL, INSTANCE_URL]
    assert (filing_directory / "xbrl" / "package.zip").exists()
    assert (filing_directory / "xbrl" / "instance.xml").exists()
    assert any(line.startswith("[DOWNLOADED] AAPL/10-Q/") for line in output)


def test_download_xbrl_packages_reports_absence_without_failure(tmp_path: Path) -> None:
    _write_filing_metadata(tmp_path)
    client = FakeClient(payload=_index_payload("filing.htm"))

    summary = download_xbrl_packages(
        client=client, download_root=tmp_path, printer=lambda _: None
    )

    assert summary.processed == 1
    assert summary.not_available == 1
    assert summary.failed == 0


def test_download_xbrl_packages_repairs_partial_local_state(tmp_path: Path) -> None:
    filing_directory = _write_filing_metadata(tmp_path)
    xbrl_directory = filing_directory / "xbrl"
    xbrl_directory.mkdir()
    (xbrl_directory / "package.zip").write_bytes(b"partial")
    client = FakeClient(
        payload=_index_payload(PACKAGE_NAME, INSTANCE_NAME),
        package_bytes=_zip_bytes(),
        instance_bytes=_instance_bytes(),
    )

    summary = download_xbrl_packages(
        client=client, download_root=tmp_path, printer=lambda _: None
    )

    assert summary.downloaded == 1
    assert (xbrl_directory / "metadata.json").exists()
    assert (xbrl_directory / "instance.xml").exists()


def test_download_xbrl_packages_counts_invalid_metadata_once(tmp_path: Path) -> None:
    filing_directory = tmp_path / "AAPL" / "10-Q" / ACCESSION
    filing_directory.mkdir(parents=True)
    (filing_directory / "metadata.json").write_text(
        json.dumps({"form": "10-Q"}), encoding="utf-8"
    )

    summary = download_xbrl_packages(
        client=FakeClient(payload={}), download_root=tmp_path, printer=lambda _: None
    )

    assert summary.files_found == 1
    assert summary.processed == 1
    assert summary.failed == 1
    assert summary.failure_reasons == {"invalid_local_metadata": 1}
