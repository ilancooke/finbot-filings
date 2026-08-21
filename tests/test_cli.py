from __future__ import annotations

import argparse

import pytest

from finbot_filings.cli import build_parser


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
