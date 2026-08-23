"""Persist compact, source-aware inventories of filing taxonomy packages."""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from finbot_filings.layout import filing_directory, form_directory
from finbot_filings.xbrl.source import (
    XBRLSource,
    inventory_xbrl_source,
    read_json_object,
)
from finbot_filings.xbrl.taxonomy.models import (
    TaxonomyPackageInventory,
    TaxonomyReference,
)
from finbot_filings.xbrl.taxonomy.package import (
    MAX_PACKAGE_MEMBERS,
    MAX_TOTAL_UNCOMPRESSED_BYTES,
    MAX_TOTAL_XML_BYTES,
    MAX_XML_COMPRESSION_RATIO,
    MAX_XML_MEMBER_BYTES,
    TaxonomyInventoryError,
    inventory_counts,
    inventory_taxonomy_package,
)

INVENTORY_SCHEMA_VERSION = 1
PARSER_VERSION = "xbrl-taxonomy-inventory-v2"
INVENTORY_FILENAME = "taxonomy_inventory.json"


@dataclass(slots=True)
class TaxonomyInventorySummary:
    files_found: int = 0
    processed: int = 0
    inventoried: int = 0
    with_warnings: int = 0
    failed: int = 0
    skipped: int = 0
    schemas: int = 0
    embedded_linkbase_documents: int = 0
    external_dependencies: int = 0
    unresolved_local_references: int = 0
    failure_reasons: Counter[str] = field(default_factory=Counter)


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _output_path(output_root: Path, metadata: dict[str, Any]) -> Path:
    return filing_directory(
        output_root,
        ticker=str(metadata["ticker"]),
        form=str(metadata["form"]),
        accession_number=str(metadata["accession_number"]),
    ) / INVENTORY_FILENAME


def _matches(
    metadata: dict[str, Any],
    *,
    ticker: str | None,
    form_type: str | None,
    accession_number: str | None,
) -> bool:
    if ticker and str(metadata.get("ticker", "")).strip().upper() != ticker.upper():
        return False
    if form_type and form_directory(str(metadata.get("form", ""))) != form_directory(
        form_type
    ):
        return False
    return not (
        accession_number
        and str(metadata.get("accession_number", "")).strip() != accession_number.strip()
    )


def _current_inventory(path: Path, source_sha256: str) -> bool:
    try:
        value = read_json_object(path)
    except ValueError:
        return False
    return (
        value.get("status") in {"success", "warning"}
        and value.get("schema_version") == INVENTORY_SCHEMA_VERSION
        and value.get("parser_version") == PARSER_VERSION
        and value.get("source_package_sha256") == source_sha256
    )


def _reference_dict(reference: TaxonomyReference) -> dict[str, Any]:
    return asdict(reference)


def _unique_dependencies(
    inventory: TaxonomyPackageInventory,
) -> list[dict[str, Any]]:
    dependencies: dict[tuple[str, str, str | None], TaxonomyReference] = {}
    for reference in inventory.external_dependencies:
        if reference.reference_type == "locator":
            continue
        key = (
            reference.reference_type,
            reference.resolved_uri,
            reference.namespace,
        )
        dependencies.setdefault(key, reference)
    return [
        _reference_dict(reference)
        for _, reference in sorted(
            dependencies.items(),
            key=lambda value: (
                value[0][0],
                value[0][1],
                value[0][2] or "",
            ),
        )
    ]


def _manifest(
    *,
    source: XBRLSource,
    inventory: TaxonomyPackageInventory,
    now: Callable[[], datetime] | None,
) -> dict[str, Any]:
    counts = inventory_counts(inventory)
    unresolved = [
        _reference_dict(reference)
        for reference in inventory.unresolved_local_references
    ]
    unknown_resources = [
        asdict(resource)
        for resource in inventory.resources
        if "other_xml" in resource.resource_types
    ]
    warnings = list(inventory.warnings)
    if unresolved:
        warnings.append(
            f"{len(unresolved)} local taxonomy references could not be resolved"
        )
    if unknown_resources:
        warnings.append(
            f"{len(unknown_resources)} XML resources have unfamiliar root elements"
        )
    warnings = sorted(set(warnings))
    inventoried_at = (now or (lambda: datetime.now(timezone.utc)))()
    non_locator_references = [
        _reference_dict(reference)
        for reference in inventory.references
        if reference.reference_type != "locator"
    ]
    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "parser": "xbrl_taxonomy_inventory",
        "parser_version": PARSER_VERSION,
        "status": "warning" if warnings else "success",
        "ticker": source.filing_metadata.get("ticker"),
        "cik": source.filing_metadata.get("cik"),
        "form": source.filing_metadata.get("form"),
        "accession_number": source.filing_metadata.get("accession_number"),
        "filing_date": source.filing_metadata.get("filing_date"),
        "report_date": source.filing_metadata.get("report_date"),
        "source_package_path": str(source.package_path),
        "source_package_sha256": source.package_sha256,
        "package_member_count": len(source.package_members),
        "inventory_limits": {
            "max_package_members": MAX_PACKAGE_MEMBERS,
            "max_total_uncompressed_bytes": MAX_TOTAL_UNCOMPRESSED_BYTES,
            "max_xml_member_bytes": MAX_XML_MEMBER_BYTES,
            "max_total_xml_bytes": MAX_TOTAL_XML_BYTES,
            "max_xml_compression_ratio": MAX_XML_COMPRESSION_RATIO,
        },
        "resources": [asdict(resource) for resource in inventory.resources],
        "references": non_locator_references,
        "role_types": [asdict(role) for role in inventory.role_types],
        "arcrole_types": [asdict(arcrole) for arcrole in inventory.arcrole_types],
        "counts": counts,
        "external_dependencies": _unique_dependencies(inventory),
        "external_reference_count": len(inventory.external_dependencies),
        "unresolved_local_references": unresolved,
        "unknown_resources": unknown_resources,
        "warnings": warnings,
        "inventoried_at": inventoried_at.astimezone(timezone.utc).isoformat(),
    }


def _failure_manifest(
    *,
    metadata: dict[str, Any],
    package_path: Path,
    source_package_sha256: str | None,
    reason: str,
    details: str,
    now: Callable[[], datetime] | None,
) -> dict[str, Any]:
    inventoried_at = (now or (lambda: datetime.now(timezone.utc)))()
    return {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "parser": "xbrl_taxonomy_inventory",
        "parser_version": PARSER_VERSION,
        "status": "failure",
        "ticker": metadata.get("ticker"),
        "cik": metadata.get("cik"),
        "form": metadata.get("form"),
        "accession_number": metadata.get("accession_number"),
        "filing_date": metadata.get("filing_date"),
        "report_date": metadata.get("report_date"),
        "source_package_path": str(package_path),
        "source_package_sha256": source_package_sha256,
        "failure_reason": reason,
        "details": details,
        "warnings": [],
        "inventoried_at": inventoried_at.astimezone(timezone.utc).isoformat(),
    }


def _label(metadata: dict[str, Any]) -> str:
    return (
        f"{str(metadata['ticker']).upper()}/{str(metadata['form']).upper()}/"
        f"{metadata['accession_number']}"
    )


def _validate_metadata_layout(
    metadata: dict[str, Any], *, metadata_path: Path, download_root: Path
) -> None:
    ticker = str(metadata["ticker"]).strip().upper()
    form = form_directory(str(metadata["form"]))
    accession = str(metadata["accession_number"]).strip()
    if not ticker or not accession:
        raise ValueError("metadata ticker and accession number must not be empty")
    expected = filing_directory(
        download_root,
        ticker=ticker,
        form=form,
        accession_number=accession,
    ) / "metadata.json"
    if metadata_path != expected:
        raise ValueError("filing metadata does not match its storage hierarchy")


def inventory_taxonomy_filings(
    *,
    download_root: Path,
    output_root: Path,
    ticker: str | None = None,
    form_type: str | None = None,
    accession_number: str | None = None,
    overwrite: bool = False,
    printer: Callable[[str], None] = print,
    now: Callable[[], datetime] | None = None,
) -> TaxonomyInventorySummary:
    """Inventory matching packages one filing at a time and persist diagnostics."""
    summary = TaxonomyInventorySummary()
    for metadata_path in sorted(download_root.glob("*/*/*/metadata.json")):
        filing_path = metadata_path.parent
        try:
            metadata = read_json_object(metadata_path)
        except (OSError, TypeError, ValueError) as exc:
            summary.files_found += 1
            summary.processed += 1
            summary.failed += 1
            summary.failure_reasons["invalid_local_metadata"] += 1
            printer(f"[FAIL] {metadata_path} — invalid_local_metadata: {exc}")
            continue
        if not _matches(
            metadata,
            ticker=ticker,
            form_type=form_type,
            accession_number=accession_number,
        ):
            continue
        summary.files_found += 1
        summary.processed += 1
        try:
            _validate_metadata_layout(
                metadata, metadata_path=metadata_path, download_root=download_root
            )
            output_path = _output_path(output_root, metadata)
            label = _label(metadata)
        except (KeyError, TypeError, ValueError) as exc:
            summary.failed += 1
            summary.failure_reasons["invalid_local_metadata"] += 1
            printer(f"[FAIL] {metadata_path} — invalid_local_metadata: {exc}")
            continue

        source: XBRLSource | None = None
        try:
            source = inventory_xbrl_source(filing_path)
            if not overwrite and _current_inventory(output_path, source.package_sha256):
                summary.processed -= 1
                summary.skipped += 1
                printer(f"[SKIP] {label} — current taxonomy inventory already exists")
                continue
            inventory = inventory_taxonomy_package(source.package_path)
            manifest = _manifest(source=source, inventory=inventory, now=now)
            _atomic_write_json(output_path, manifest)
        except TaxonomyInventoryError as exc:
            reason = exc.reason
            details = exc.details
        except (KeyError, OSError, TypeError, ValueError) as exc:
            reason = "taxonomy_inventory_error"
            details = str(exc)
        else:
            counts = manifest["counts"]
            summary.inventoried += 1
            if manifest["status"] == "warning":
                summary.with_warnings += 1
            summary.schemas += int(counts["schema_count"])
            summary.embedded_linkbase_documents += int(
                counts["embedded_linkbase_document_count"]
            )
            summary.external_dependencies += len(manifest["external_dependencies"])
            summary.unresolved_local_references += len(
                manifest["unresolved_local_references"]
            )
            prefix = "WARNING" if manifest["status"] == "warning" else "INVENTORIED"
            printer(
                f"[{prefix}] {label} — {counts['schema_count']} schemas; "
                f"{sum(counts['relationship_counts'].values())} relationships; "
                f"{len(manifest['external_dependencies'])} external dependencies"
            )
            continue

        summary.failed += 1
        summary.failure_reasons[reason] += 1
        try:
            failure = _failure_manifest(
                metadata=metadata,
                package_path=filing_path / "xbrl" / "package.zip",
                source_package_sha256=(source.package_sha256 if source else None),
                reason=reason,
                details=details,
                now=now,
            )
            _atomic_write_json(output_path, failure)
        except (OSError, TypeError, ValueError):
            pass
        printer(f"[FAIL] {label} — {reason}: {details}")

    printer("")
    printer("Taxonomy inventory summary")
    printer("--------------------------")
    printer(f"Filings found:               {summary.files_found}")
    printer(f"Processed:                   {summary.processed}")
    printer(f"Inventoried:                 {summary.inventoried}")
    printer(f"With warnings:               {summary.with_warnings}")
    printer(f"Skipped:                     {summary.skipped}")
    printer(f"Failed:                      {summary.failed}")
    printer(f"Schemas:                     {summary.schemas}")
    printer(
        f"Embedded linkbase documents: {summary.embedded_linkbase_documents}"
    )
    printer(f"External dependencies:       {summary.external_dependencies}")
    printer(
        f"Unresolved local references: {summary.unresolved_local_references}"
    )
    if summary.failure_reasons:
        printer("")
        printer("Failure reasons:")
        for reason, count in sorted(summary.failure_reasons.items()):
            printer(f"  {reason}: {count}")
    return summary
