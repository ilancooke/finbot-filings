"""Official SEC URL construction adapted from the legacy discovery module."""

from urllib.parse import quote

from finbot_ingestion.domain.identity import (
    normalize_accession_number,
    normalize_cik,
    validate_filename,
)

ARCHIVES_BASE_URL = "https://www.sec.gov/Archives/edgar/data"


def company_submissions_url(cik: str | int) -> str:
    return f"https://data.sec.gov/submissions/CIK{normalize_cik(cik)}.json"


def accession_directory_url(cik: str | int, accession_number: str) -> str:
    accession = normalize_accession_number(accession_number).replace("-", "")
    return f"{ARCHIVES_BASE_URL}/{int(normalize_cik(cik))}/{accession}"


def filing_index_url(cik: str | int, accession_number: str) -> str:
    accession = normalize_accession_number(accession_number)
    return f"{accession_directory_url(cik, accession)}/{accession}-index.html"


def document_url(cik: str | int, accession_number: str, filename: str) -> str:
    name = quote(validate_filename(filename), safe="._-")
    return f"{accession_directory_url(cik, accession_number)}/{name}"
