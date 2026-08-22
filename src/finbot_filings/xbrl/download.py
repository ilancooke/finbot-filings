"""Discover and store SEC-generated XBRL packages for downloaded filings."""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

from lxml import etree

from finbot_filings.layout import form_directory
from finbot_filings.sec.client import SECClient, SECDataError, SECError
from finbot_filings.sec.filings import ARCHIVES_BASE_URL, normalize_accession_number

XBRL_METADATA_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class XBRLPackageLocation:
    index_url: str
    package_url: str
    source_filename: str
    instance_url: str
    instance_filename: str


@dataclass(frozen=True, slots=True)
class XBRLPackagePaths:
    directory: Path
    package: Path
    instance: Path
    metadata: Path


@dataclass(slots=True)
class XBRLDownloadSummary:
    files_found: int = 0
    processed: int = 0
    downloaded: int = 0
    skipped: int = 0
    not_available: int = 0
    failed: int = 0
    failure_reasons: Counter[str] = field(default_factory=Counter)


def accession_directory_url(cik: int, accession_number: str) -> str:
    normalized_accession = normalize_accession_number(accession_number)
    return f"{ARCHIVES_BASE_URL}/{int(cik)}/{normalized_accession}"


def accession_index_json_url(cik: int, accession_number: str) -> str:
    return f"{accession_directory_url(cik, accession_number)}/index.json"


def _directory_items(payload: Any, *, url: str) -> Sequence[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        raise SECDataError(f"SEC accession index must be a JSON object: {url}")
    directory = payload.get("directory")
    if not isinstance(directory, Mapping):
        raise SECDataError(f"SEC accession index has no directory object: {url}")
    items = directory.get("item")
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise SECDataError(f"SEC accession index has no item array: {url}")
    return [item for item in items if isinstance(item, Mapping)]


def discover_xbrl_package(
    client: SECClient,
    *,
    cik: int,
    accession_number: str,
) -> XBRLPackageLocation | None:
    """Return the SEC XBRL package and generated instance for an accession."""
    index_url = accession_index_json_url(cik, accession_number)
    items = _directory_items(client.get_json(index_url), url=index_url)
    filenames = []
    instance_filenames = []
    for item in items:
        name = str(item.get("name", "")).strip()
        lower_name = name.lower()
        if lower_name.endswith("-xbrl.zip") or lower_name.endswith("_htm.xml"):
            if Path(name).name != name:
                raise SECDataError(
                    f"SEC accession index contains an unsafe XBRL filename: {name!r}"
                )
        if lower_name.endswith("-xbrl.zip"):
            filenames.append(name)
        elif lower_name.endswith("_htm.xml"):
            instance_filenames.append(name)
    if not filenames:
        return None
    if len(filenames) != 1:
        raise SECDataError(
            f"SEC accession index lists multiple XBRL packages: {index_url}"
        )
    if len(instance_filenames) != 1:
        raise SECDataError(
            "SEC accession index must list exactly one generated XBRL instance: "
            f"{index_url}"
        )
    source_filename = filenames[0]
    instance_filename = instance_filenames[0]
    directory_url = accession_directory_url(cik, accession_number)
    package_url = (
        f"{directory_url}/{quote(source_filename, safe='._-')}"
    )
    instance_url = f"{directory_url}/{quote(instance_filename, safe='._-')}"
    return XBRLPackageLocation(
        index_url=index_url,
        package_url=package_url,
        source_filename=source_filename,
        instance_url=instance_url,
        instance_filename=instance_filename,
    )


def _inspect_xbrl_zip(package_bytes: bytes, *, source_url: str) -> tuple[int, list[str]]:
    try:
        with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
            corrupt_member = archive.testzip()
            if corrupt_member is not None:
                raise SECDataError(
                    f"SEC XBRL package contains a corrupt member {corrupt_member!r}: "
                    f"{source_url}"
                )
            names = archive.namelist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError) as exc:
        raise SECDataError(f"SEC returned an invalid XBRL ZIP: {source_url}") from exc
    instance_candidates = sorted(
        name for name in names if name.lower().endswith("_htm.xml")
    )
    return len(names), instance_candidates


def _inspect_instance_xml(instance_bytes: bytes, *, source_url: str) -> None:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    try:
        root = etree.fromstring(instance_bytes, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise SECDataError(f"SEC returned invalid XBRL instance XML: {source_url}") from exc
    if root.tag != "{http://www.xbrl.org/2003/instance}xbrl":
        raise SECDataError(f"SEC returned a non-XBRL instance document: {source_url}")


def xbrl_paths_for(filing_directory: Path) -> XBRLPackagePaths:
    directory = filing_directory / "xbrl"
    return XBRLPackagePaths(
        directory=directory,
        package=directory / "package.zip",
        instance=directory / "instance.xml",
        metadata=directory / "metadata.json",
    )


def xbrl_download_complete(filing_directory: Path) -> bool:
    """Return whether all durable raw XBRL acquisition artifacts exist."""
    paths = xbrl_paths_for(filing_directory)
    return paths.package.exists() and paths.instance.exists() and paths.metadata.exists()


def _atomic_write_bytes(path: Path, value: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(value)
    os.replace(temporary, path)


def _atomic_write_text(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def store_xbrl_package(
    *,
    filing_directory: Path,
    filing_metadata: Mapping[str, Any],
    location: XBRLPackageLocation,
    package_bytes: bytes,
    instance_bytes: bytes,
    overwrite: bool = False,
    now: Callable[[], datetime] | None = None,
) -> XBRLPackagePaths:
    """Store exact SEC package/instance bytes plus integrity metadata."""
    paths = xbrl_paths_for(filing_directory)
    if (
        paths.package.exists()
        and paths.instance.exists()
        and paths.metadata.exists()
        and not overwrite
    ):
        return paths
    member_count, instance_candidates = _inspect_xbrl_zip(
        package_bytes, source_url=location.package_url
    )
    _inspect_instance_xml(instance_bytes, source_url=location.instance_url)
    paths.directory.mkdir(parents=True, exist_ok=True)
    downloaded_at = (now or (lambda: datetime.now(timezone.utc)))()
    metadata = {
        "schema_version": XBRL_METADATA_SCHEMA_VERSION,
        "ticker": filing_metadata.get("ticker"),
        "cik": filing_metadata.get("cik"),
        "form": filing_metadata.get("form"),
        "accession_number": filing_metadata.get("accession_number"),
        "source_index_url": location.index_url,
        "source_package_url": location.package_url,
        "source_filename": location.source_filename,
        "sha256": hashlib.sha256(package_bytes).hexdigest(),
        "byte_count": len(package_bytes),
        "member_count": member_count,
        "package_instance_document_candidates": instance_candidates,
        "instance_source_url": location.instance_url,
        "instance_source_filename": location.instance_filename,
        "instance_sha256": hashlib.sha256(instance_bytes).hexdigest(),
        "instance_byte_count": len(instance_bytes),
        "downloaded_at": downloaded_at.astimezone(timezone.utc).isoformat(),
    }
    _atomic_write_bytes(paths.package, package_bytes)
    _atomic_write_bytes(paths.instance, instance_bytes)
    _atomic_write_text(
        paths.metadata,
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
    )
    return paths


def download_xbrl_packages(
    *,
    client: SECClient,
    download_root: Path,
    form_type: str | None = None,
    ticker: str | None = None,
    overwrite: bool = False,
    printer: Callable[[str], None] = print,
) -> XBRLDownloadSummary:
    """Download XBRL packages for locally downloaded filings in stable order."""
    normalized_form = form_directory(form_type) if form_type else None
    normalized_ticker = ticker.strip().upper() if ticker else None
    summary = XBRLDownloadSummary()
    for metadata_path in sorted(download_root.glob("*/*/*/metadata.json")):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            filing_form = form_directory(str(metadata.get("form", "")))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            summary.files_found += 1
            summary.processed += 1
            summary.failed += 1
            summary.failure_reasons["invalid_local_metadata"] += 1
            printer(f"[FAIL] {metadata_path} — invalid_local_metadata: {exc}")
            continue

        if normalized_form is not None and filing_form != normalized_form:
            continue
        metadata_ticker = str(metadata.get("ticker", "")).strip().upper()
        if normalized_ticker is not None and metadata_ticker != normalized_ticker:
            continue
        summary.files_found += 1
        try:
            ticker = str(metadata["ticker"]).strip().upper()
            cik = int(metadata["cik"])
            accession = str(metadata["accession_number"]).strip()
            if not ticker or metadata_path.parent.parent.parent.name != ticker:
                raise ValueError("metadata ticker does not match its ticker directory")
            if metadata_path.parent.parent.name != filing_form:
                raise ValueError("metadata form does not match its form directory")
            if metadata_path.parent.name != accession:
                raise ValueError(
                    "metadata accession number does not match its accession directory"
                )
            if cik <= 0:
                raise ValueError("metadata CIK must be positive")
        except (KeyError, TypeError, ValueError) as exc:
            summary.processed += 1
            summary.failed += 1
            summary.failure_reasons["invalid_local_metadata"] += 1
            printer(f"[FAIL] {metadata_path} — invalid_local_metadata: {exc}")
            continue

        label = f"{ticker}/{filing_form}/{accession}"
        paths = xbrl_paths_for(metadata_path.parent)
        if (
            paths.package.exists()
            and paths.instance.exists()
            and paths.metadata.exists()
            and not overwrite
        ):
            summary.skipped += 1
            printer(f"[SKIP] {label} — XBRL package already exists")
            continue

        summary.processed += 1
        try:
            location = discover_xbrl_package(
                client, cik=cik, accession_number=accession
            )
            if location is None:
                summary.not_available += 1
                printer(f"[NO-XBRL] {label} — no SEC XBRL package listed")
                continue
            package_bytes = client.get_bytes(location.package_url)
            instance_bytes = client.get_bytes(location.instance_url)
            stored = store_xbrl_package(
                filing_directory=metadata_path.parent,
                filing_metadata=metadata,
                location=location,
                package_bytes=package_bytes,
                instance_bytes=instance_bytes,
                overwrite=overwrite,
            )
        except (SECError, OSError, ValueError) as exc:
            summary.failed += 1
            summary.failure_reasons["xbrl_download_error"] += 1
            printer(f"[FAIL] {label} — xbrl_download_error: {exc}")
            continue

        summary.downloaded += 1
        printer(f"[DOWNLOADED] {label} — {stored.package} + {stored.instance.name}")

    printer("")
    printer("XBRL download summary")
    printer("---------------------")
    printer(f"Filings found:  {summary.files_found}")
    printer(f"Processed:      {summary.processed}")
    printer(f"Downloaded:     {summary.downloaded}")
    printer(f"Already present: {summary.skipped}")
    printer(f"No XBRL:        {summary.not_available}")
    printer(f"Failed:         {summary.failed}")
    if summary.failure_reasons:
        printer("")
        printer("Failure reasons:")
        for reason, count in sorted(summary.failure_reasons.items()):
            printer(f"  {reason}: {count}")
    return summary
