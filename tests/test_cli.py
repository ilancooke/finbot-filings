from __future__ import annotations

import argparse
import logging

import pytest

import finbot_filings.cli as cli
from finbot_filings.cli import build_parser
from finbot_filings.models import Company


def test_cli_rejects_unsupported_form() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["discover", "AAPL", "--form", "8-K"])


@pytest.mark.parametrize("count", ["0", "-3"])
def test_cli_rejects_non_positive_count(count: str) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["discover", "AAPL", "--form", "10-K", "--count", count]
        )


def test_cli_accepts_case_insensitive_supported_form() -> None:
    args = build_parser().parse_args(["discover", "aapl", "--form", "10-k"])
    assert args.form == "10-K"
    assert args.count == 5


def test_cli_accepts_download_folder_override(tmp_path) -> None:
    args = build_parser().parse_args(
        [
            "download",
            "AAPL",
            "--form",
            "10-K",
            "--download-folder",
            str(tmp_path),
        ]
    )
    assert args.download_folder == tmp_path


def test_cli_accepts_parse_section_folders_and_form(tmp_path) -> None:
    output_path = tmp_path / "sections"
    args = build_parser().parse_args(
        [
            "parse-sections",
            "--input-folder",
            str(tmp_path),
            "--output-folder",
            str(output_path),
            "--form",
            "10-q",
            "--overwrite",
            "--max-chunk-chars",
            "12000",
            "--chunk-overlap-chars",
            "500",
        ]
    )
    assert args.input_folder == tmp_path
    assert args.output_folder == output_path
    assert args.form == "10-Q"
    assert args.overwrite is True
    assert args.max_chunk_chars == 12000
    assert args.chunk_overlap_chars == 500


def test_cli_reports_no_matching_filings_as_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(cli, "SECClient", lambda: object())
    monkeypatch.setattr(cli, "sec_cik_overrides", lambda: {})
    monkeypatch.setattr(
        cli,
        "discover_filings",
        lambda *args, **kwargs: (Company("NONE", "No Filings Inc.", 123), []),
    )
    caplog.set_level(logging.ERROR)

    exit_code = cli.main(["discover", "NONE", "--form", "10-K", "--count", "1"])

    assert exit_code == 1
    assert "No exact 10-K filings found for NONE" in caplog.text
