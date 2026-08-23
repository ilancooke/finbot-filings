from __future__ import annotations

import argparse
import logging
from types import SimpleNamespace

import pytest

import finbot_filings.cli as cli
from finbot_filings.cli import build_parser
from finbot_filings.models import Company, Filing


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
        ]
    )
    assert args.input_folder == tmp_path
    assert args.output_folder == output_path
    assert args.form == "10-Q"
    assert args.overwrite is True


def test_cli_accepts_xbrl_download_options(tmp_path) -> None:
    args = build_parser().parse_args(
        [
            "download-xbrl",
            "AAPL",
            "--download-folder",
            str(tmp_path),
            "--form",
            "10-q",
            "--overwrite",
        ]
    )
    assert args.download_folder == tmp_path
    assert args.ticker == "AAPL"
    assert args.form == "10-Q"
    assert args.overwrite is True


def test_cli_dispatches_xbrl_download(monkeypatch, tmp_path) -> None:
    captured = {}

    class Summary:
        failed = 0

    monkeypatch.setattr(cli, "SECClient", lambda: "client")

    def fake_download(**kwargs):
        captured.update(kwargs)
        return Summary()

    monkeypatch.setattr(cli, "download_xbrl_packages", fake_download)

    exit_code = cli.main(
        ["download-xbrl", "--download-folder", str(tmp_path), "--form", "10-K"]
    )

    assert exit_code == 0
    assert captured == {
        "client": "client",
        "download_root": tmp_path,
        "form_type": "10-K",
        "ticker": None,
        "overwrite": False,
    }


def test_cli_accepts_xbrl_extract_and_show_options(tmp_path) -> None:
    extract = build_parser().parse_args(
        [
            "extract-xbrl",
            "aapl",
            "--form",
            "10-k",
            "--download-folder",
            str(tmp_path / "raw"),
            "--output-folder",
            str(tmp_path / "derived"),
            "--overwrite",
        ]
    )
    assert extract.ticker == "aapl"
    assert extract.form == "10-K"
    assert extract.overwrite is True

    show = build_parser().parse_args(
        [
            "show-xbrl",
            "AAPL",
            "--concept",
            "Assets",
            "--format",
            "json",
            "--limit",
            "3",
        ]
    )
    assert show.concept == "Assets"
    assert show.format == "json"
    assert show.limit == 3


def test_cli_accepts_taxonomy_inventory_options(tmp_path) -> None:
    args = build_parser().parse_args(
        [
            "inventory-taxonomy",
            "aapl",
            "--form",
            "10-k",
            "--accession",
            "0000320193-25-000079",
            "--download-folder",
            str(tmp_path / "raw"),
            "--output-folder",
            str(tmp_path / "derived"),
            "--overwrite",
        ]
    )

    assert args.ticker == "aapl"
    assert args.form == "10-K"
    assert args.accession == "0000320193-25-000079"
    assert args.overwrite is True


def test_cli_dispatches_taxonomy_inventory_without_sec_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    captured = {}

    class Summary:
        failed = 0

    def fail_client():
        raise AssertionError("taxonomy inventory must not construct SECClient")

    def fake_inventory(**kwargs):
        captured.update(kwargs)
        return Summary()

    monkeypatch.setattr(cli, "SECClient", fail_client)
    monkeypatch.setattr(cli, "inventory_taxonomy_filings", fake_inventory)

    exit_code = cli.main(
        [
            "inventory-taxonomy",
            "AAPL",
            "--form",
            "10-K",
            "--download-folder",
            str(tmp_path / "raw"),
            "--output-folder",
            str(tmp_path / "derived"),
        ]
    )

    assert exit_code == 0
    assert captured == {
        "download_root": tmp_path / "raw",
        "output_root": tmp_path / "derived",
        "ticker": "AAPL",
        "form_type": "10-K",
        "accession_number": None,
        "overwrite": False,
    }


def test_cli_accepts_taxonomy_extract_options(tmp_path) -> None:
    args = build_parser().parse_args(
        [
            "extract-taxonomy",
            "aapl",
            "--form",
            "10-q",
            "--accession",
            "0000320193-26-000020",
            "--download-folder",
            str(tmp_path / "raw"),
            "--output-folder",
            str(tmp_path / "derived"),
            "--overwrite",
        ]
    )

    assert args.ticker == "aapl"
    assert args.form == "10-Q"
    assert args.accession == "0000320193-26-000020"
    assert args.overwrite is True


def test_cli_dispatches_taxonomy_extract_without_sec_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    captured = {}

    class Summary:
        failed = 0

    def fail_client():
        raise AssertionError("taxonomy extraction must not construct SECClient")

    def fake_extract(**kwargs):
        captured.update(kwargs)
        return Summary()

    monkeypatch.setattr(cli, "SECClient", fail_client)
    monkeypatch.setattr(cli, "extract_taxonomy_filings", fake_extract)

    exit_code = cli.main(
        [
            "extract-taxonomy",
            "AAPL",
            "--form",
            "10-K",
            "--download-folder",
            str(tmp_path / "raw"),
            "--output-folder",
            str(tmp_path / "derived"),
        ]
    )

    assert exit_code == 0
    assert captured == {
        "download_root": tmp_path / "raw",
        "output_root": tmp_path / "derived",
        "ticker": "AAPL",
        "form_type": "10-K",
        "accession_number": None,
        "overwrite": False,
    }


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


def test_cli_download_uses_unified_filing_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    filing: Filing,
) -> None:
    client = object()
    captured = {}
    monkeypatch.setattr(cli, "SECClient", lambda: client)
    monkeypatch.setattr(cli, "sec_cik_overrides", lambda: {})
    monkeypatch.setattr(
        cli,
        "discover_filings",
        lambda *args, **kwargs: (
            Company(filing.ticker, filing.company_name, filing.cik),
            [filing],
        ),
    )

    def fake_bundle(**kwargs):
        captured.update(kwargs)
        paths = kwargs["storage"].paths_for(kwargs["filing"])
        return SimpleNamespace(
            paths=paths,
            skipped=False,
            document_acquisition_method="xbrl_package_member",
            xbrl_status="downloaded",
        )

    monkeypatch.setattr(cli, "download_filing_bundle", fake_bundle)

    exit_code = cli.main(
        [
            "download",
            "AAPL",
            "--form",
            "10-K",
            "--count",
            "1",
            "--download-folder",
            str(tmp_path),
        ]
    )

    assert exit_code == 0
    assert captured["client"] is client
    assert captured["filing"] is filing
    assert captured["storage"].download_folder == tmp_path
    assert captured["overwrite"] is False
