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
    directory = root / ticker / form / accession
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

    manifest_path = output_root / "TEST" / "10-Q" / accession / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert summary.succeeded == 1
    assert summary.failed == 0
    assert summary.complete == 1
    assert summary.native_sections_extracted == 11
    assert summary.canonical_sections_mapped == 11
    assert summary.semantic_only_sections == 0
    assert summary.completeness_rate == 100.0
    assert manifest["schema_version"] == 3
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
    assert any(
        f"TEST/10-Q/{accession}/filing.html" in line for line in output
    )


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


def test_batch_partitions_same_ticker_by_form(tmp_path: Path) -> None:
    input_root = tmp_path / "raw"
    output_root = tmp_path / "sections"
    ten_q_accession = "0000123456-26-000020"
    ten_k_accession = "0000123456-26-000021"
    create_download(
        input_root,
        ticker="SAME",
        accession=ten_q_accession,
        fixture="normal_10q.html",
        form="10-Q",
    )
    create_download(
        input_root,
        ticker="SAME",
        accession=ten_k_accession,
        fixture="normal_10k.html",
        form="10-K",
    )

    summary = parse_downloaded_filings(
        input_root=input_root,
        output_root=output_root,
        printer=lambda _: None,
    )

    assert summary.succeeded == 2
    assert (
        output_root / "SAME" / "10-Q" / ten_q_accession / "manifest.json"
    ).is_file()
    assert (
        output_root / "SAME" / "10-K" / ten_k_accession / "manifest.json"
    ).is_file()


def test_batch_rejects_metadata_in_the_wrong_form_directory(tmp_path: Path) -> None:
    input_root = tmp_path / "raw"
    output_root = tmp_path / "sections"
    accession = "0000123456-26-000022"
    document = create_download(
        input_root,
        ticker="WRONG",
        accession=accession,
        fixture="normal_10q.html",
        form="10-Q",
    )
    wrong_directory = input_root / "WRONG" / "10-K" / accession
    wrong_directory.parent.mkdir(parents=True, exist_ok=True)
    document.parent.rename(wrong_directory)

    summary = parse_downloaded_filings(
        input_root=input_root,
        output_root=output_root,
        printer=lambda _: None,
    )

    assert summary.failed == 1
    assert summary.failure_reasons["storage_layout_mismatch"] == 1


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
    directory = input_root / "FAIL" / "10-Q" / accession
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
    output_directory = output_root / "FAIL" / "10-Q" / accession
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


def test_overwrite_removes_retired_chunk_output(tmp_path: Path) -> None:
    input_root = tmp_path / "raw"
    output_root = tmp_path / "sections"
    accession = "0000123456-26-000003"
    create_download(
        input_root,
        ticker="TEST",
        accession=accession,
        fixture="normal_10q.html",
    )
    retired_chunk = (
        output_root / "TEST" / "10-Q" / accession / "chunks" / "item7" / "001.txt"
    )
    retired_chunk.parent.mkdir(parents=True)
    retired_chunk.write_text("retired chunk", encoding="utf-8")

    parse_downloaded_filings(
        input_root=input_root,
        output_root=output_root,
        form_type="10-Q",
        overwrite=True,
        printer=lambda _: None,
    )

    output_directory = output_root / "TEST" / "10-Q" / accession
    manifest = json.loads((output_directory / "manifest.json").read_text())
    assert not (output_directory / "chunks").exists()
    assert "chunks" not in manifest
