from __future__ import annotations

import json
import shutil
from pathlib import Path

from finbot_filings.parsing.batch import parse_downloaded_filings

FIXTURES = Path(__file__).parent / "fixtures"


def create_download(
    root: Path,
    *,
    ticker: str,
    accession: str,
    fixture: str,
    form: str = "10-Q",
) -> Path:
    directory = root / ticker / accession
    directory.mkdir(parents=True)
    document = directory / "filing.html"
    shutil.copyfile(FIXTURES / fixture, document)
    (directory / "metadata.json").write_text(
        json.dumps(
            {
                "ticker": ticker,
                "company_name": f"{ticker} Company",
                "cik": 123456,
                "form": form,
                "accession_number": accession,
                "filing_date": "2026-08-01",
                "report_date": "2026-06-30",
                "primary_document": "filing.htm",
                "filing_url": "https://example.test/index",
                "document_url": "https://example.test/document",
                "downloaded_at": "2026-08-02T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    return document


def test_batch_writes_section_files_and_inspection_manifest(tmp_path: Path) -> None:
    input_root = tmp_path / "raw"
    output_root = tmp_path / "sections"
    accession = "0000123456-26-000001"
    document = create_download(
        input_root,
        ticker="TEST",
        accession=accession,
        fixture="normal_10q.html",
    )
    output: list[str] = []

    summary = parse_downloaded_filings(
        input_root=input_root,
        output_root=output_root,
        form_type="10-Q",
        printer=output.append,
    )

    manifest_path = output_root / "TEST" / accession / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert summary.succeeded == 1
    assert summary.failed == 0
    assert summary.complete == 1
    assert summary.native_sections_extracted == 11
    assert summary.canonical_sections_mapped == 11
    assert summary.semantic_only_sections == 0
    assert summary.completeness_rate == 100.0
    assert manifest["schema_version"] == 2
    assert manifest["parser"] == "native_toc"
    assert manifest["status"] == "success"
    assert manifest["source_filing_path"] == str(document)
    assert len(manifest["source_sha256"]) == 64
    assert manifest["sections_found"] == 11
    assert manifest["canonical_mapping"]["status"] == "complete"
    assert manifest["recognized_toc_entries"][0]["dom_order"] >= 0
    assert all(section["character_count"] > 0 for section in manifest["sections"])
    assert all(
        (manifest_path.parent / section["text_file"]).is_file()
        for section in manifest["sections"]
    )
    assert any(line.startswith("[PASS]") for line in output)


def test_batch_summary_reports_native_extraction_and_mapping_counts(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "raw"
    output_root = tmp_path / "sections"
    create_download(
        input_root,
        ticker="FULL",
        accession="0000123456-26-000010",
        fixture="normal_10q.html",
    )
    create_download(
        input_root,
        ticker="PARTIAL",
        accession="0000123456-26-000011",
        fixture="physical_order_10q.html",
    )
    output: list[str] = []

    summary = parse_downloaded_filings(
        input_root=input_root,
        output_root=output_root,
        form_type="10-Q",
        printer=output.append,
    )

    assert summary.processed == 2
    assert summary.succeeded == 2
    assert summary.success_rate == 100.0
    assert summary.complete == 2
    assert summary.completeness_rate == 100.0
    assert summary.native_sections_extracted == 18
    assert summary.canonical_sections_mapped == 18
    assert "Native extraction rate: 100.0%" in output
    assert "Complete files:        2/2 (100.0%)" in output
    assert "Native sections:       18" in output
    assert "Exact mappings:        18" in output


def test_partial_extraction_is_not_reported_as_complete(tmp_path: Path) -> None:
    input_root = tmp_path / "raw"
    output_root = tmp_path / "sections"
    create_download(
        input_root,
        ticker="PARTIAL",
        accession="0000123456-26-000012",
        fixture="missing_anchor_10q.html",
    )
    output: list[str] = []

    summary = parse_downloaded_filings(
        input_root=input_root,
        output_root=output_root,
        form_type="10-Q",
        printer=output.append,
    )

    assert summary.partial == 1
    assert summary.succeeded == 0
    assert summary.failed == 0
    assert summary.complete == 0
    assert summary.success_rate == 100.0
    assert summary.completeness_rate == 0.0
    assert any(line.startswith("[PARTIAL]") for line in output)


def test_batch_failure_writes_only_diagnostic_manifest_and_then_skips(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "raw"
    output_root = tmp_path / "sections"
    accession = "0000123456-26-000002"
    directory = input_root / "FAIL" / accession
    directory.mkdir(parents=True)
    (directory / "filing.html").write_text(
        "<html><body><h2>Item 1 without a TOC link</h2></body></html>",
        encoding="utf-8",
    )
    (directory / "metadata.json").write_text(
        json.dumps(
            {
                "ticker": "FAIL",
                "cik": 123456,
                "form": "10-Q",
                "accession_number": accession,
                "filing_date": "2026-08-01",
                "report_date": "2026-06-30",
            }
        ),
        encoding="utf-8",
    )

    first = parse_downloaded_filings(
        input_root=input_root, output_root=output_root, printer=lambda _: None
    )
    output_directory = output_root / "FAIL" / accession
    manifest = json.loads((output_directory / "manifest.json").read_text())
    second = parse_downloaded_filings(
        input_root=input_root, output_root=output_root, printer=lambda _: None
    )

    assert first.failed == 1
    assert manifest["status"] == "failure"
    assert manifest["failure_reason"] == "no_toc_found"
    assert list(output_directory.glob("*.txt")) == []
    assert second.skipped == 1
    assert second.processed == 0


def test_batch_writes_section_local_chunks_with_provenance(tmp_path: Path) -> None:
    input_root = tmp_path / "raw"
    output_root = tmp_path / "sections"
    accession = "0000123456-26-000003"
    create_download(
        input_root,
        ticker="CHUNK",
        accession=accession,
        fixture="normal_10q.html",
    )

    summary = parse_downloaded_filings(
        input_root=input_root,
        output_root=output_root,
        form_type="10-Q",
        max_chunk_chars=20,
        chunk_overlap_chars=5,
        printer=lambda _: None,
    )

    output_directory = output_root / "CHUNK" / accession
    manifest = json.loads((output_directory / "manifest.json").read_text())
    assert summary.chunks_written == len(manifest["chunks"])
    assert summary.chunks_written > 0
    assert all(
        (output_directory / chunk["text_file"]).is_file()
        for chunk in manifest["chunks"]
    )
    section_ids = {section["source_section_id"] for section in manifest["sections"]}
    assert {chunk["source_section_id"] for chunk in manifest["chunks"]} <= section_ids
