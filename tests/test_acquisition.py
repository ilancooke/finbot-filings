from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from finbot_filings.acquisition import (
    download_filing_bundle,
    primary_document_from_package,
)
from finbot_filings.models import Filing
from finbot_filings.sec.client import SECDataError
from finbot_filings.sec.filings import ARCHIVES_BASE_URL
from finbot_filings.storage.local import LocalFilingStorage


class FakeClient:
    def __init__(self, *, payload: Any, byte_values: dict[str, bytes]) -> None:
        self.payload = payload
        self.byte_values = byte_values
        self.json_calls: list[str] = []
        self.byte_calls: list[str] = []

    def get_json(self, url: str) -> Any:
        self.json_calls.append(url)
        return self.payload

    def get_bytes(self, url: str) -> bytes:
        self.byte_calls.append(url)
        return self.byte_values[url]


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    value = io.BytesIO()
    with zipfile.ZipFile(value, "w") as archive:
        for name, contents in files.items():
            archive.writestr(name, contents)
    return value.getvalue()


def _instance_bytes() -> bytes:
    return b'<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"/>'


def _urls(filing: Filing) -> tuple[str, str, str, str, str]:
    directory = (
        f"{ARCHIVES_BASE_URL}/{filing.cik}/"
        f"{filing.accession_number.replace('-', '')}"
    )
    package_name = f"{filing.accession_number}-xbrl.zip"
    instance_name = "aapl-20250927_htm.xml"
    return (
        f"{directory}/index.json",
        f"{directory}/{package_name}",
        package_name,
        f"{directory}/{instance_name}",
        instance_name,
    )


def _payload(package_name: str | None, instance_name: str | None) -> dict[str, object]:
    names = [name for name in (package_name, instance_name) if name]
    return {"directory": {"item": [{"name": name} for name in names]}}


def test_primary_document_from_package_returns_exact_member() -> None:
    package = _zip_bytes({"filing.htm": b"<html>exact</html>", "schema.xsd": b"x"})
    assert primary_document_from_package(
        package, primary_document="filing.htm"
    ) == (b"<html>exact</html>", "filing.htm")


def test_primary_document_from_package_rejects_unsafe_or_ambiguous_names() -> None:
    package = _zip_bytes(
        {"one/filing.htm": b"one", "two/filing.htm": b"two"}
    )
    with pytest.raises(SECDataError, match="multiple primary-document"):
        primary_document_from_package(package, primary_document="filing.htm")
    with pytest.raises(SECDataError, match="unsafe SEC primary"):
        primary_document_from_package(package, primary_document="../filing.htm")


def test_bundle_uses_package_member_without_primary_document_request(
    tmp_path: Path, filing: Filing
) -> None:
    index_url, package_url, package_name, instance_url, instance_name = _urls(filing)
    html = b"<html>from package</html>"
    client = FakeClient(
        payload=_payload(package_name, instance_name),
        byte_values={
            package_url: _zip_bytes({filing.primary_document: html}),
            instance_url: _instance_bytes(),
        },
    )

    result = download_filing_bundle(
        client=client,
        filing=filing,
        storage=LocalFilingStorage(tmp_path),
    )

    assert result.document_acquisition_method == "xbrl_package_member"
    assert result.xbrl_status == "downloaded"
    assert result.paths.document.read_bytes() == html
    assert client.json_calls == [index_url]
    assert client.byte_calls == [package_url, instance_url]
    metadata = json.loads(result.paths.metadata.read_text(encoding="utf-8"))
    assert metadata["document_acquisition_method"] == "xbrl_package_member"
    assert metadata["document_package_member"] == filing.primary_document
    assert metadata["document_source_package_url"] == package_url
    assert (result.paths.directory / "xbrl" / "package.zip").exists()
    assert (result.paths.directory / "xbrl" / "instance.xml").exists()


def test_bundle_falls_back_to_primary_url_when_xbrl_is_unavailable(
    tmp_path: Path, filing: Filing
) -> None:
    index_url, _, _, _, _ = _urls(filing)
    client = FakeClient(
        payload=_payload(None, None),
        byte_values={filing.document_url: b"<html>direct</html>"},
    )

    result = download_filing_bundle(
        client=client,
        filing=filing,
        storage=LocalFilingStorage(tmp_path),
    )

    assert result.document_acquisition_method == "primary_document_url"
    assert result.xbrl_status == "not_available"
    assert result.paths.document.read_bytes() == b"<html>direct</html>"
    assert client.json_calls == [index_url]
    assert client.byte_calls == [filing.document_url]


def test_bundle_falls_back_when_package_omits_primary_document(
    tmp_path: Path, filing: Filing
) -> None:
    _, package_url, package_name, instance_url, instance_name = _urls(filing)
    client = FakeClient(
        payload=_payload(package_name, instance_name),
        byte_values={
            package_url: _zip_bytes({"schema.xsd": b"x"}),
            instance_url: _instance_bytes(),
            filing.document_url: b"<html>direct fallback</html>",
        },
    )

    result = download_filing_bundle(
        client=client,
        filing=filing,
        storage=LocalFilingStorage(tmp_path),
    )

    assert result.document_acquisition_method == "primary_document_url"
    assert result.xbrl_status == "downloaded"
    assert client.byte_calls == [package_url, instance_url, filing.document_url]


def test_bundle_backfills_xbrl_without_replacing_existing_html(
    tmp_path: Path, filing: Filing
) -> None:
    storage = LocalFilingStorage(tmp_path)
    storage.store(filing, b"existing html")
    _, package_url, package_name, instance_url, instance_name = _urls(filing)
    client = FakeClient(
        payload=_payload(package_name, instance_name),
        byte_values={
            package_url: _zip_bytes({filing.primary_document: b"package html"}),
            instance_url: _instance_bytes(),
        },
    )

    result = download_filing_bundle(client=client, filing=filing, storage=storage)

    assert result.document_written is False
    assert result.document_acquisition_method == "existing"
    assert result.xbrl_status == "downloaded"
    assert result.paths.document.read_bytes() == b"existing html"
    assert filing.document_url not in client.byte_calls


def test_complete_bundle_is_skipped_without_sec_requests(
    tmp_path: Path, filing: Filing
) -> None:
    storage = LocalFilingStorage(tmp_path)
    _, package_url, package_name, instance_url, instance_name = _urls(filing)
    first_client = FakeClient(
        payload=_payload(package_name, instance_name),
        byte_values={
            package_url: _zip_bytes({filing.primary_document: b"package html"}),
            instance_url: _instance_bytes(),
        },
    )
    download_filing_bundle(client=first_client, filing=filing, storage=storage)
    second_client = FakeClient(payload={}, byte_values={})

    result = download_filing_bundle(
        client=second_client, filing=filing, storage=storage
    )

    assert result.skipped is True
    assert second_client.json_calls == []
    assert second_client.byte_calls == []


def test_missing_html_is_recovered_from_existing_local_package(
    tmp_path: Path, filing: Filing
) -> None:
    storage = LocalFilingStorage(tmp_path)
    _, package_url, package_name, instance_url, instance_name = _urls(filing)
    first_client = FakeClient(
        payload=_payload(package_name, instance_name),
        byte_values={
            package_url: _zip_bytes({filing.primary_document: b"package html"}),
            instance_url: _instance_bytes(),
        },
    )
    first = download_filing_bundle(client=first_client, filing=filing, storage=storage)
    first.paths.document.unlink()
    first.paths.metadata.unlink()
    second_client = FakeClient(payload={}, byte_values={})

    result = download_filing_bundle(
        client=second_client, filing=filing, storage=storage
    )

    assert result.document_written is True
    assert result.document_acquisition_method == "xbrl_package_member"
    assert result.paths.document.read_bytes() == b"package html"
    assert second_client.json_calls == []
    assert second_client.byte_calls == []
