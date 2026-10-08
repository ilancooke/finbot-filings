"""Batch parsing and inspectable local output for downloaded SEC filings."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from finbot_filings.layout import filing_directory, form_directory
from finbot_filings.parsing.definitions import FORM_DEFINITIONS
from finbot_filings.parsing.models import (
    FailureReason,
    FilingParseResult,
    ParseDiagnostics,
    ParseStatus,
)
from finbot_filings.parsing.toc import parse_filing_sections

MANIFEST_SCHEMA_VERSION = 4
PARSER_VERSION = "native-toc-v3"


@dataclass(slots=True)
class BatchParseSummary:
    files_found: int = 0
    processed: int = 0
    succeeded: int = 0
    partial: int = 0
    failed: int = 0
    skipped: int = 0
    complete: int = 0
    native_sections_extracted: int = 0
    native_outline_entries_detected: int = 0
    native_outline_entries_extracted: int = 0
    canonical_sections_mapped: int = 0
    semantic_only_sections: int = 0
    unmapped_sections: int = 0
    failure_reasons: Counter[str] = field(default_factory=Counter)

    @property
    def success_rate(self) -> float:
        completed = self.succeeded + self.partial
        return (completed / self.processed * 100.0) if self.processed else 0.0

    @property
    def completeness_rate(self) -> float:
        return (self.complete / self.processed * 100.0) if self.processed else 0.0

    @property
    def native_outline_coverage(self) -> float:
        if self.native_outline_entries_detected == 0:
            return 0.0
        return (
            self.native_outline_entries_extracted
            / self.native_outline_entries_detected
            * 100.0
        )


def _atomic_write_text(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def _result_manifest(
    result: FilingParseResult,
    *,
    source_metadata: dict[str, Any],
    source_sha256: str,
    section_files: dict[str, str],
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "parser": "native_toc",
        "parser_version": PARSER_VERSION,
        "source_filing_path": str(result.file),
        "source_identifier": source_metadata.get("accession_number"),
        "source_sha256": source_sha256,
        "ticker": source_metadata.get("ticker"),
        "cik": source_metadata.get("cik"),
        "accession_number": source_metadata.get("accession_number"),
        "filing_date": source_metadata.get("filing_date"),
        "report_date": source_metadata.get("report_date"),
        "form_type": result.form_type,
        "status": result.status.value,
        "source_extraction_status": result.status.value,
        "sections_found": result.sections_found,
        "native_sections_found": result.sections_found,
        "native_outline": {
            "entries_detected": result.diagnostics.native_outline_entries_detected,
            "entries_extracted": result.diagnostics.native_outline_entries_extracted,
            "entries_skipped": result.diagnostics.native_outline_entries_skipped,
            "coverage": result.diagnostics.native_outline_coverage,
        },
        "canonical_mapping": {
            "status": result.mapping_status.value,
            "exact_mappings": result.canonical_sections_mapped,
            "semantic_only": result.semantic_only_sections,
            "unmapped": result.unmapped_sections,
        },
        "recognized_toc_entries": [asdict(entry) for entry in result.recognized_toc_entries],
        "diagnostics": result.diagnostics.to_dict(),
        "warnings": result.warnings,
        "sections": [
            {
                "section_id": section.section_id,
                "source_section_id": section.source_section_id,
                "source_part": section.part,
                "source_item": section.item,
                "source_title": section.source_title,
                "part": section.part,
                "item": section.item,
                "title": section.title,
                "canonical_section_id": section.canonical_section_id,
                "canonical_mapping_method": section.canonical_mapping_method,
                "semantic_categories": list(section.semantic_categories),
                "registrant_name": section.registrant_name,
                "registrant_identity_source": section.registrant_identity_source,
                "anchor_id": section.anchor_id,
                "physical_order": section.physical_order,
                "character_count": len(section.text),
                "text_file": section_files[section.source_section_id],
            }
            for section in result.sections
        ],
    }
    if result.failure_reason is not None:
        manifest["failure_reason"] = result.failure_reason.value
    if result.details is not None:
        manifest["details"] = result.details
    return manifest


def write_parse_result(
    result: FilingParseResult,
    *,
    source_metadata: dict[str, Any],
    source_sha256: str,
    output_root: Path,
    replace_existing: bool = False,
) -> Path:
    """Write section text files and a filing-level diagnostic manifest."""
    ticker = str(source_metadata.get("ticker", "UNKNOWN")).strip().upper() or "UNKNOWN"
    accession = str(source_metadata.get("accession_number", result.file.parent.name))
    output_directory = filing_directory(
        output_root,
        ticker=ticker,
        form=result.form_type,
        accession_number=accession,
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    if replace_existing:
        for old_file in output_directory.glob("*.txt"):
            old_file.unlink()
        # Remove both current section output and any chunk directories created
        # by the retired schema-v2 workflow.
        for generated_directory in ("sections", "chunks"):
            path = output_directory / generated_directory
            if path.is_dir():
                shutil.rmtree(path)

    sections_directory = output_directory / "sections"
    sections_directory.mkdir(parents=True, exist_ok=True)
    section_files: dict[str, str] = {}
    for section in result.sections:
        filename = f"{section.physical_order:02d}_{section.source_section_id}.txt"
        relative_path = str(Path("sections") / filename)
        section_files[section.source_section_id] = relative_path
        _atomic_write_text(sections_directory / filename, section.text + "\n")

    manifest = _result_manifest(
        result,
        source_metadata=source_metadata,
        source_sha256=source_sha256,
        section_files=section_files,
    )
    manifest_path = output_directory / "manifest.json"
    _atomic_write_text(
        manifest_path,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    return manifest_path


def _failed_read_result(file: Path, form_type: str, details: str) -> FilingParseResult:
    return FilingParseResult(
        file=file,
        form_type=form_type,
        status=ParseStatus.FAILURE,
        diagnostics=ParseDiagnostics(),
        failure_reason=FailureReason.PARSE_ERROR,
        details=details,
    )


def parse_downloaded_filings(
    *,
    input_root: Path,
    output_root: Path,
    form_type: str | None = None,
    overwrite: bool = False,
    printer: Callable[[str], None] = print,
) -> BatchParseSummary:
    """Parse downloaded filings in stable path order and persist inspectable results."""
    normalized_form = form_type.strip().upper() if form_type else None
    if normalized_form is not None and normalized_form not in FORM_DEFINITIONS:
        raise ValueError("form_type must be 10-Q or 10-K")
    summary = BatchParseSummary()
    metadata_paths = sorted(input_root.glob("*/*/*/metadata.json"))
    for metadata_path in metadata_paths:
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            printer(f"[FAIL] {metadata_path} — parse_error")
            summary.files_found += 1
            summary.processed += 1
            summary.failed += 1
            summary.failure_reasons[FailureReason.PARSE_ERROR.value] += 1
            continue

        filing_form = str(metadata.get("form", "")).strip().upper()
        if normalized_form is not None and filing_form != normalized_form:
            continue
        if filing_form not in FORM_DEFINITIONS:
            continue

        summary.files_found += 1
        ticker = str(metadata.get("ticker", "UNKNOWN")).strip().upper() or "UNKNOWN"
        accession = str(metadata.get("accession_number", metadata_path.parent.name))
        directory_form = metadata_path.parent.parent.name
        if directory_form != form_directory(filing_form):
            printer(f"[FAIL] {metadata_path} — storage_layout_mismatch")
            summary.processed += 1
            summary.failed += 1
            summary.failure_reasons[
                FailureReason.STORAGE_LAYOUT_MISMATCH.value
            ] += 1
            continue
        document_path = metadata_path.parent / "filing.html"
        output_directory = filing_directory(
            output_root,
            ticker=ticker,
            form=filing_form,
            accession_number=accession,
        )
        manifest_path = output_directory / "manifest.json"
        label = f"{ticker}/{filing_form}/{accession}/{document_path.name}"
        if manifest_path.exists() and not overwrite:
            printer(f"[SKIP] {label} — manifest already exists")
            summary.skipped += 1
            continue

        summary.processed += 1
        try:
            document_bytes = document_path.read_bytes()
            source_sha256 = hashlib.sha256(document_bytes).hexdigest()
            result = parse_filing_sections(
                document_bytes,
                file=document_path,
                form_type=filing_form,
            )
        except OSError as exc:
            document_bytes = b""
            source_sha256 = ""
            result = _failed_read_result(document_path, filing_form, str(exc))

        write_parse_result(
            result,
            source_metadata=metadata,
            source_sha256=source_sha256,
            output_root=output_root,
            replace_existing=overwrite,
        )
        summary.native_outline_entries_detected += (
            result.diagnostics.native_outline_entries_detected
        )
        summary.native_outline_entries_extracted += (
            result.diagnostics.native_outline_entries_extracted
        )
        if result.status in {ParseStatus.SUCCESS, ParseStatus.PARTIAL}:
            exact = result.canonical_sections_mapped
            semantic_only = result.semantic_only_sections
            printer(
                f"[{'PASS' if result.status is ParseStatus.SUCCESS else 'PARTIAL'}] "
                f"{label} — {result.sections_found} native sections; "
                f"{exact} exact mappings; {semantic_only} semantic-only"
            )
            if result.status is ParseStatus.SUCCESS:
                summary.succeeded += 1
                summary.complete += 1
            else:
                summary.partial += 1
            summary.native_sections_extracted += result.sections_found
            summary.canonical_sections_mapped += exact
            summary.semantic_only_sections += semantic_only
            summary.unmapped_sections += result.unmapped_sections
        else:
            reason = (
                result.failure_reason.value
                if result.failure_reason is not None
                else FailureReason.PARSE_ERROR.value
            )
            printer(f"[FAIL] {label} — {reason}")
            summary.failed += 1
            summary.failure_reasons[reason] += 1

    printer("")
    printer("Parsing summary")
    printer("---------------")
    printer(f"Files found:     {summary.files_found}")
    printer(f"Files processed: {summary.processed}")
    printer(f"Extraction succeeded: {summary.succeeded}")
    printer(f"Extraction partial:   {summary.partial}")
    printer(f"Extraction failed:    {summary.failed}")
    printer(f"Skipped:         {summary.skipped}")
    printer(f"Native extraction rate: {summary.success_rate:.1f}%")
    printer(
        f"Complete files:        {summary.complete}/{summary.processed} "
        f"({summary.completeness_rate:.1f}%)"
    )
    printer(f"Native sections:       {summary.native_sections_extracted}")
    printer(
        "Native outline coverage: "
        f"{summary.native_outline_entries_extracted}/"
        f"{summary.native_outline_entries_detected} "
        f"({summary.native_outline_coverage:.1f}%)"
    )
    printer(f"Exact mappings:        {summary.canonical_sections_mapped}")
    printer(f"Semantic-only:         {summary.semantic_only_sections}")
    printer(f"Unmapped sections:     {summary.unmapped_sections}")
    if summary.failure_reasons:
        printer("")
        printer("Failure reasons:")
        for reason, count in sorted(summary.failure_reasons.items()):
            printer(f"  {reason}: {count}")
    return summary
