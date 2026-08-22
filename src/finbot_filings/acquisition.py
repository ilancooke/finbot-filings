"""Unified acquisition of filing HTML and available SEC XBRL inputs."""

from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from finbot_filings.models import Filing
from finbot_filings.sec.client import SECClient, SECDataError
from finbot_filings.storage.local import FilingPaths, LocalFilingStorage
from finbot_filings.xbrl.download import (
    discover_xbrl_package,
    store_xbrl_package,
    xbrl_download_complete,
    xbrl_paths_for,
)


@dataclass(frozen=True, slots=True)
class FilingBundleResult:
    paths: FilingPaths
    skipped: bool
    document_written: bool
    document_acquisition_method: str
    xbrl_status: str


def primary_document_from_package(
    package_bytes: bytes,
    *,
    primary_document: str,
) -> tuple[bytes, str] | None:
    """Return the SEC primary document from its XBRL package, when present."""
    primary_document = primary_document.strip()
    requested_member = PurePosixPath(primary_document)
    if (
        not primary_document
        or requested_member.is_absolute()
        or ".." in requested_member.parts
    ):
        raise SECDataError(f"unsafe SEC primary document name: {primary_document!r}")
    try:
        with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
            matches: list[str] = []
            for name in archive.namelist():
                member = PurePosixPath(name)
                if member.is_absolute() or ".." in member.parts:
                    raise SECDataError(f"unsafe XBRL package member: {name!r}")
                if name == primary_document:
                    matches.append(name)
            if not matches:
                matches = [
                    name
                    for name in archive.namelist()
                    if PurePosixPath(name).name == requested_member.name
                ]
            if not matches:
                return None
            if len(matches) != 1:
                raise SECDataError(
                    f"XBRL package contains multiple primary-document matches: "
                    f"{primary_document!r}"
                )
            try:
                value = archive.read(matches[0])
            except RuntimeError as exc:
                raise SECDataError("cannot read SEC XBRL package member") from exc
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise SECDataError("invalid SEC XBRL package") from exc
    if not value:
        raise SECDataError(
            f"XBRL package primary document is empty: {primary_document!r}"
        )
    return value, matches[0]


def _existing_package_source_url(filing_directory: Path) -> str | None:
    metadata_path = xbrl_paths_for(filing_directory).metadata
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = metadata.get("source_package_url")
    return str(value) if value else None


def download_filing_bundle(
    *,
    client: SECClient,
    filing: Filing,
    storage: LocalFilingStorage,
    overwrite: bool = False,
) -> FilingBundleResult:
    """Acquire HTML from the package when possible and retain XBRL raw inputs."""
    filing_paths = storage.paths_for(filing)
    document_complete = storage.is_complete(filing)
    xbrl_complete = xbrl_download_complete(filing_paths.directory)
    if document_complete and xbrl_complete and not overwrite:
        return FilingBundleResult(
            paths=filing_paths,
            skipped=True,
            document_written=False,
            document_acquisition_method="existing",
            xbrl_status="existing",
        )

    package_bytes: bytes | None = None
    package_source_url: str | None = None
    xbrl_status = "existing" if xbrl_complete and not overwrite else "not_available"
    if xbrl_complete and not overwrite:
        package_bytes = xbrl_paths_for(filing_paths.directory).package.read_bytes()
        package_source_url = _existing_package_source_url(filing_paths.directory)
    else:
        location = discover_xbrl_package(
            client,
            cik=filing.cik,
            accession_number=filing.accession_number,
        )
        if location is not None:
            package_bytes = client.get_bytes(location.package_url)
            instance_bytes = client.get_bytes(location.instance_url)
            store_xbrl_package(
                filing_directory=filing_paths.directory,
                filing_metadata=filing.to_dict(),
                location=location,
                package_bytes=package_bytes,
                instance_bytes=instance_bytes,
                overwrite=overwrite,
            )
            package_source_url = location.package_url
            xbrl_status = "downloaded"

    document_method = "existing"
    document_member: str | None = None
    document_bytes: bytes | None = None
    if not document_complete or overwrite:
        if package_bytes is not None:
            packaged_document = primary_document_from_package(
                package_bytes,
                primary_document=filing.primary_document,
            )
            if packaged_document is not None:
                document_bytes, document_member = packaged_document
                document_method = "xbrl_package_member"
        if document_bytes is None:
            document_bytes = client.get_bytes(filing.document_url)
            document_method = "primary_document_url"

        metadata_updates: dict[str, str] = {
            "document_acquisition_method": document_method,
        }
        if document_member is not None:
            metadata_updates["document_package_member"] = document_member
            if package_source_url is not None:
                metadata_updates["document_source_package_url"] = package_source_url
        stored = storage.store(
            filing,
            document_bytes,
            overwrite=overwrite,
            metadata_updates=metadata_updates,
        )
        document_written = stored.written
    else:
        document_written = False

    return FilingBundleResult(
        paths=filing_paths,
        skipped=False,
        document_written=document_written,
        document_acquisition_method=document_method,
        xbrl_status=xbrl_status,
    )
