"""Inventory, normalize, and persist downloaded XBRL facts."""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pyarrow as pa
import pyarrow.parquet as pq

from finbot_filings.layout import filing_directory, form_directory
from finbot_filings.xbrl.instance import InstanceParseResult, parse_instance_document
from finbot_filings.xbrl.source import (
    XBRLExtractionError,
    XBRLSource,
    inventory_xbrl_source,
    read_json_object,
)

FACT_SCHEMA_VERSION = 1
EXTRACTION_METADATA_SCHEMA_VERSION = 1
PARSER_VERSION = "sec-generated-instance-v1"

FACT_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("ticker", pa.string(), nullable=False),
        pa.field("cik", pa.int64(), nullable=False),
        pa.field("form", pa.string(), nullable=False),
        pa.field("accession_number", pa.string(), nullable=False),
        pa.field("filing_date", pa.date32()),
        pa.field("report_date", pa.date32()),
        pa.field("instance_document", pa.string(), nullable=False),
        pa.field("document_order", pa.int64(), nullable=False),
        pa.field("fact_id", pa.string()),
        pa.field("concept_qname", pa.string(), nullable=False),
        pa.field("concept_namespace", pa.string(), nullable=False),
        pa.field("concept_name", pa.string(), nullable=False),
        pa.field("concept_prefix", pa.string()),
        pa.field("value_text", pa.large_string(), nullable=False),
        pa.field("value_kind", pa.string(), nullable=False),
        pa.field("context_id", pa.string(), nullable=False),
        pa.field("entity_identifier", pa.string(), nullable=False),
        pa.field("entity_scheme", pa.string()),
        pa.field("context_period_kind", pa.string(), nullable=False),
        pa.field("period_start", pa.date32()),
        pa.field("period_end", pa.date32()),
        pa.field("period_instant", pa.date32()),
        pa.field("unit_id", pa.string()),
        pa.field("unit_json", pa.string()),
        pa.field("unit_display", pa.string()),
        pa.field("dimensions_json", pa.string(), nullable=False),
        pa.field("context_signature", pa.string(), nullable=False),
        pa.field("decimals", pa.string()),
        pa.field("precision", pa.string()),
        pa.field("is_nil", pa.bool_(), nullable=False),
        pa.field("xml_language", pa.string()),
        pa.field("duplicate_group_id", pa.string(), nullable=False),
        pa.field("duplicate_group_size", pa.int32(), nullable=False),
    ]
)


@dataclass(frozen=True, slots=True)
class XBRLDerivedPaths:
    directory: Path
    facts: Path
    metadata: Path


@dataclass(slots=True)
class XBRLExtractionSummary:
    files_found: int = 0
    processed: int = 0
    extracted: int = 0
    skipped: int = 0
    failed: int = 0
    facts_written: int = 0
    failure_reasons: Counter[str] = field(default_factory=Counter)


def _read_json(path: Path) -> dict[str, Any]:
    return read_json_object(path)


def _derived_paths(output_root: Path, metadata: dict[str, Any]) -> XBRLDerivedPaths:
    directory = filing_directory(
        output_root,
        ticker=str(metadata["ticker"]),
        form=str(metadata["form"]),
        accession_number=str(metadata["accession_number"]),
    )
    return XBRLDerivedPaths(
        directory=directory,
        facts=directory / "facts.parquet",
        metadata=directory / "metadata.json",
    )


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def write_extracted_facts(
    *,
    source: XBRLSource,
    result: InstanceParseResult,
    output_root: Path,
    now: Callable[[], datetime] | None = None,
) -> XBRLDerivedPaths:
    paths = _derived_paths(output_root, source.filing_metadata)
    paths.directory.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(result.facts, schema=FACT_SCHEMA)
    temporary = paths.facts.with_name(f".{paths.facts.name}.tmp")
    pq.write_table(table, temporary, compression="zstd")
    os.replace(temporary, paths.facts)
    extracted_at = (now or (lambda: datetime.now(timezone.utc)))()
    metadata = {
        "schema_version": EXTRACTION_METADATA_SCHEMA_VERSION,
        "fact_schema_version": FACT_SCHEMA_VERSION,
        "parser": "xbrl_instance",
        "parser_version": PARSER_VERSION,
        "ticker": source.filing_metadata.get("ticker"),
        "cik": source.filing_metadata.get("cik"),
        "form": source.filing_metadata.get("form"),
        "accession_number": source.filing_metadata.get("accession_number"),
        "filing_date": source.filing_metadata.get("filing_date"),
        "report_date": source.filing_metadata.get("report_date"),
        "source_package_path": str(source.package_path),
        "source_package_sha256": source.package_sha256,
        "source_instance_path": str(source.instance_path),
        "source_instance_filename": source.acquisition_metadata.get(
            "instance_source_filename"
        ),
        "source_instance_url": source.acquisition_metadata.get(
            "instance_source_url"
        ),
        "source_instance_sha256": source.instance_sha256,
        "package_member_count": len(source.package_members),
        "fact_count": len(result.facts),
        "context_count": result.context_count,
        "unit_count": result.unit_count,
        "namespace_count": result.namespace_count,
        "duplicate_group_count": result.duplicate_group_count,
        "value_kind_counts": result.value_kind_counts,
        "facts_file": paths.facts.name,
        "extracted_at": extracted_at.astimezone(timezone.utc).isoformat(),
    }
    _atomic_write_json(paths.metadata, metadata)
    return paths


def _source_metadata_paths(download_root: Path) -> list[Path]:
    return sorted(download_root.glob("*/*/*/xbrl/metadata.json"))


def _matches(
    metadata: dict[str, Any], *, ticker: str | None, form_type: str | None
) -> bool:
    if ticker and str(metadata.get("ticker", "")).strip().upper() != ticker.upper():
        return False
    if form_type and form_directory(str(metadata.get("form", ""))) != form_directory(
        form_type
    ):
        return False
    return True


def extract_xbrl_filings(
    *,
    download_root: Path,
    output_root: Path,
    ticker: str | None = None,
    form_type: str | None = None,
    overwrite: bool = False,
    printer: Callable[[str], None] = print,
) -> XBRLExtractionSummary:
    """Normalize matching raw XBRL instances into isolated Parquet datasets."""
    summary = XBRLExtractionSummary()
    for acquisition_path in _source_metadata_paths(download_root):
        filing_directory_path = acquisition_path.parent.parent
        try:
            filing_metadata = _read_json(filing_directory_path / "metadata.json")
            if not _matches(filing_metadata, ticker=ticker, form_type=form_type):
                continue
            summary.files_found += 1
            paths = _derived_paths(output_root, filing_metadata)
            label = (
                f"{str(filing_metadata['ticker']).upper()}/"
                f"{str(filing_metadata['form']).upper()}/"
                f"{filing_metadata['accession_number']}"
            )
            if paths.facts.exists() and paths.metadata.exists() and not overwrite:
                summary.skipped += 1
                printer(f"[SKIP] {label} — extracted facts already exist")
                continue
            summary.processed += 1
            source = inventory_xbrl_source(filing_directory_path)
            result = parse_instance_document(
                source.instance_bytes,
                filing_metadata=source.filing_metadata,
                instance_filename=str(
                    source.acquisition_metadata["instance_source_filename"]
                ),
            )
            written = write_extracted_facts(
                source=source, result=result, output_root=output_root
            )
        except (KeyError, OSError, TypeError, ValueError) as exc:
            summary.failed += 1
            summary.failure_reasons["xbrl_extraction_error"] += 1
            printer(f"[FAIL] {filing_directory_path} — xbrl_extraction_error: {exc}")
            continue
        summary.extracted += 1
        summary.facts_written += len(result.facts)
        printer(f"[PASS] {label} — {len(result.facts)} facts → {written.facts}")

    printer("")
    printer("XBRL extraction summary")
    printer("-----------------------")
    printer(f"Filings found:     {summary.files_found}")
    printer(f"Processed:         {summary.processed}")
    printer(f"Extracted:         {summary.extracted}")
    printer(f"Skipped:           {summary.skipped}")
    printer(f"Failed:            {summary.failed}")
    printer(f"Facts written:     {summary.facts_written}")
    return summary


def inspect_xbrl_filings(
    *,
    download_root: Path,
    ticker: str | None = None,
    form_type: str | None = None,
    printer: Callable[[str], None] = print,
) -> int:
    """Print integrity and structural counts without writing derived data."""
    inspected = 0
    for acquisition_path in _source_metadata_paths(download_root):
        filing_directory_path = acquisition_path.parent.parent
        metadata = _read_json(filing_directory_path / "metadata.json")
        if not _matches(metadata, ticker=ticker, form_type=form_type):
            continue
        source = inventory_xbrl_source(filing_directory_path)
        result = parse_instance_document(
            source.instance_bytes,
            filing_metadata=metadata,
            instance_filename=str(source.acquisition_metadata["instance_source_filename"]),
        )
        label = (
            f"{str(metadata['ticker']).upper()}/{str(metadata['form']).upper()}/"
            f"{metadata['accession_number']}"
        )
        printer(
            f"[XBRL] {label} — {len(source.package_members)} package members; "
            f"{len(result.facts)} facts; {result.context_count} contexts; "
            f"{result.unit_count} units; {result.duplicate_group_count} duplicate groups"
        )
        inspected += 1
    return inspected
