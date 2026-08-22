"""Canonical local layout for raw filings and derived section output."""

from __future__ import annotations

from pathlib import Path

SUPPORTED_FORM_DIRECTORIES = frozenset({"10-K", "10-Q"})


def form_directory(form: str) -> str:
    """Return the canonical form directory name after strict validation."""
    normalized = form.strip().upper()
    if normalized not in SUPPORTED_FORM_DIRECTORIES:
        raise ValueError(f"form must be 10-K or 10-Q, not {form!r}")
    return normalized


def filing_directory(
    root: Path,
    *,
    ticker: str,
    form: str,
    accession_number: str,
) -> Path:
    """Return ROOT/TICKER/FORM/ACCESSION using canonical directory names."""
    normalized_ticker = ticker.strip().upper()
    if not normalized_ticker:
        raise ValueError("ticker must not be empty")
    normalized_accession = accession_number.strip()
    if not normalized_accession:
        raise ValueError("accession_number must not be empty")
    return Path(root) / normalized_ticker / form_directory(form) / normalized_accession
