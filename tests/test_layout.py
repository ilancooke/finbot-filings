from __future__ import annotations

from pathlib import Path

import pytest

from finbot_filings.layout import filing_directory, form_directory


@pytest.mark.parametrize(
    ("value", "expected"),
    [("10-k", "10-K"), (" 10-Q ", "10-Q")],
)
def test_form_directory_normalizes_supported_forms(value: str, expected: str) -> None:
    assert form_directory(value) == expected


def test_form_directory_rejects_unsupported_forms() -> None:
    with pytest.raises(ValueError, match="form must be 10-K or 10-Q"):
        form_directory("8-K")


def test_filing_directory_includes_ticker_form_and_accession() -> None:
    assert filing_directory(
        Path("/data/raw/filings"),
        ticker="aapl",
        form="10-k",
        accession_number="0000320193-25-000079",
    ) == Path("/data/raw/filings/AAPL/10-K/0000320193-25-000079")
