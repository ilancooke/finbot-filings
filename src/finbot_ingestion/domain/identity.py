"""Canonical identities; no content hashing or ticker-based identity."""

import re
from urllib.parse import unquote

SUPPORTED_FORMS = frozenset({"8-K", "10-Q", "10-K", "8-K/A", "10-Q/A", "10-K/A"})
ACCESSION_PATTERN = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}")


def normalize_cik(value: str | int) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("CIK must be a positive integer or digit string")
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,10}", text) or int(text) == 0:
        raise ValueError("CIK must contain 1–10 digits and be positive")
    return text.zfill(10)


def normalize_ticker(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("ticker must be a nonempty string")
    return value.strip().upper()


def normalize_accession_number(value: str) -> str:
    """Keep the canonical dashed identity (unlike the legacy URL helper)."""
    if not isinstance(value, str) or not ACCESSION_PATTERN.fullmatch(value.strip()):
        raise ValueError("invalid SEC accession number")
    return value.strip()


def normalize_form(value: str) -> str:
    if not isinstance(value, str) or value.strip().upper() not in SUPPORTED_FORMS:
        raise ValueError("unsupported SEC form")
    return value.strip().upper()


def validate_filename(value: str) -> str:
    """Accept an original basename, never a path or an encoded path."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("filename must be a nonempty original basename")
    decoded = unquote(value)
    for name in (value, decoded):
        if name in {".", ".."} or any(c in name for c in "/\\"):
            raise ValueError("unsafe document filename")
        if any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise ValueError("unsafe document filename")
    return value


def artifact_identity(accession_number: str, filename: str) -> str:
    # '/' cannot occur in either component, so this is unambiguous and reversible.
    return f"{normalize_accession_number(accession_number)}/{validate_filename(filename)}"


def artifact_key(cik: str | int, accession_number: str, filename: str) -> str:
    return f"{normalize_cik(cik)}/{artifact_identity(accession_number, filename)}"
