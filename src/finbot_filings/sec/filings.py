"""Company resolution, SEC submissions parsing, selection, and URL construction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence
from urllib.parse import quote

from finbot_filings.models import Company, Filing
from finbot_filings.sec.client import SECClient, SECDataError, SECError

TICKER_MAPPING_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_BASE_URL = "https://data.sec.gov/submissions"
SUBMISSIONS_URL_TEMPLATE = f"{SUBMISSIONS_BASE_URL}/CIK{{cik:010d}}.json"
ARCHIVES_BASE_URL = "https://www.sec.gov/Archives/edgar/data"
SUPPORTED_FORMS = frozenset({"10-K", "10-Q"})
ACCESSION_PATTERN = re.compile(r"^\d{10}-\d{2}-\d{6}$")
HISTORICAL_SUBMISSION_FILE_PATTERN = re.compile(
    r"^CIK\d{10}-submissions-\d{3}\.json$"
)


class TickerNotFoundError(SECError):
    """Raised when the SEC ticker map does not contain a requested ticker."""


@dataclass(frozen=True, slots=True)
class HistoricalSubmissionFile:
    name: str
    filing_from: date
    filing_to: date


def normalize_ticker(ticker: str) -> str:
    normalized = ticker.strip().upper()
    if not normalized:
        raise ValueError("ticker must not be empty")
    return normalized


def validate_form(form: str) -> str:
    normalized = form.strip().upper()
    if normalized not in SUPPORTED_FORMS:
        raise ValueError(
            f"unsupported form {form!r}; supported forms are 10-K and 10-Q"
        )
    return normalized


def validate_count(count: int) -> int:
    if count <= 0:
        raise ValueError("count must be greater than zero")
    return count


def normalize_accession_number(accession_number: str) -> str:
    """Return the dash-free accession path component after validating its shape."""
    accession_number = accession_number.strip()
    if not ACCESSION_PATTERN.fullmatch(accession_number):
        raise ValueError(f"invalid SEC accession number: {accession_number!r}")
    return accession_number.replace("-", "")


def filing_index_url(cik: int, accession_number: str) -> str:
    normalized = normalize_accession_number(accession_number)
    return (
        f"{ARCHIVES_BASE_URL}/{int(cik)}/{normalized}/"
        f"{accession_number}-index.html"
    )


def primary_document_url(
    cik: int, accession_number: str, primary_document: str
) -> str:
    normalized = normalize_accession_number(accession_number)
    document = primary_document.strip()
    if not document:
        raise ValueError("primary_document must not be empty")
    encoded_document = quote(document, safe="/._-")
    return f"{ARCHIVES_BASE_URL}/{int(cik)}/{normalized}/{encoded_document}"


def historical_submissions_url(filename: str) -> str:
    """Construct an official SEC URL for a referenced historical metadata file."""
    normalized = filename.strip()
    if not HISTORICAL_SUBMISSION_FILE_PATTERN.fullmatch(normalized):
        raise ValueError(f"invalid SEC historical submissions filename: {filename!r}")
    return f"{SUBMISSIONS_BASE_URL}/{quote(normalized, safe='._-')}"


def resolve_company(
    client: SECClient, ticker: str, *, cik_override: int | None = None
) -> Company:
    normalized_ticker = normalize_ticker(ticker)
    payload = client.get_json(TICKER_MAPPING_URL)
    if not isinstance(payload, Mapping):
        raise SECDataError("SEC ticker mapping must be a JSON object")

    for raw_company in payload.values():
        if not isinstance(raw_company, Mapping):
            continue
        mapped_ticker = str(raw_company.get("ticker", "")).strip().upper()
        if mapped_ticker == normalized_ticker:
            try:
                return Company(
                    ticker=mapped_ticker,
                    name=str(raw_company["title"]).strip(),
                    cik=cik_override or int(raw_company["cik_str"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise SECDataError(
                    f"SEC ticker mapping record for {normalized_ticker} is invalid"
                ) from exc
    raise TickerNotFoundError(f"ticker {normalized_ticker!r} was not found in SEC data")


def parse_filing_arrays(
    company: Company,
    filing_arrays: Mapping[str, Any],
    *,
    source: str,
    requested_form: str | None = None,
) -> list[Filing]:
    """Convert SEC column-oriented filing arrays into typed filing records."""
    required_columns = (
        "accessionNumber",
        "filingDate",
        "reportDate",
        "form",
        "primaryDocument",
    )
    columns: dict[str, Sequence[Any]] = {}
    for column in required_columns:
        value = filing_arrays.get(column)
        if not isinstance(value, list):
            raise SECDataError(f"SEC {source}.{column} must be an array")
        columns[column] = value

    lengths = {len(values) for values in columns.values()}
    if len(lengths) != 1:
        raise SECDataError(f"SEC {source} arrays have inconsistent lengths")

    filings: list[Filing] = []
    for index in range(next(iter(lengths), 0)):
        form = str(columns["form"][index]).strip()
        if requested_form is not None and form != requested_form:
            continue
        try:
            accession = str(columns["accessionNumber"][index]).strip()
            filing_date = date.fromisoformat(str(columns["filingDate"][index]))
            report_date_text = str(columns["reportDate"][index]).strip()
            report_date = date.fromisoformat(report_date_text) if report_date_text else None
            primary_document = str(columns["primaryDocument"][index]).strip()
            filings.append(
                Filing(
                    ticker=company.ticker,
                    company_name=company.name,
                    cik=company.cik,
                    form=form,
                    accession_number=accession,
                    filing_date=filing_date,
                    report_date=report_date,
                    primary_document=primary_document,
                    filing_url=filing_index_url(company.cik, accession),
                    document_url=primary_document_url(
                        company.cik, accession, primary_document
                    ),
                )
            )
        except (TypeError, ValueError) as exc:
            raise SECDataError(
                f"invalid SEC filing in {source} at index {index}: {exc}"
            ) from exc
    return filings


def parse_recent_filings(company: Company, recent: Mapping[str, Any]) -> list[Filing]:
    """Convert the SEC's column-oriented filings.recent object into records."""
    return parse_filing_arrays(company, recent, source="filings.recent")


def parse_historical_file_references(
    filings_object: Mapping[str, Any],
) -> list[HistoricalSubmissionFile]:
    """Parse and order SEC historical-file references from newest to oldest."""
    raw_files = filings_object.get("files", [])
    if not isinstance(raw_files, list):
        raise SECDataError("SEC filings.files must be an array")

    references: list[HistoricalSubmissionFile] = []
    for index, raw_file in enumerate(raw_files):
        if not isinstance(raw_file, Mapping):
            raise SECDataError(f"SEC filings.files[{index}] must be an object")
        try:
            name = str(raw_file["name"]).strip()
            historical_submissions_url(name)
            references.append(
                HistoricalSubmissionFile(
                    name=name,
                    filing_from=date.fromisoformat(str(raw_file["filingFrom"])),
                    filing_to=date.fromisoformat(str(raw_file["filingTo"])),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SECDataError(
                f"invalid SEC historical file reference at index {index}: {exc}"
            ) from exc
    return sorted(
        references,
        key=lambda reference: (
            reference.filing_to,
            reference.filing_from,
            reference.name,
        ),
        reverse=True,
    )


def _deduplicate_filings(filings: Sequence[Filing]) -> list[Filing]:
    unique: dict[str, Filing] = {}
    for filing in filings:
        unique.setdefault(filing.accession_number, filing)
    return list(unique.values())


def select_filings(filings: Sequence[Filing], form: str, count: int) -> list[Filing]:
    requested_form = validate_form(form)
    validate_count(count)
    exact_matches = [filing for filing in filings if filing.form == requested_form]
    return sorted(
        exact_matches,
        key=lambda filing: (filing.filing_date, filing.accession_number),
        reverse=True,
    )[:count]


def discover_filings(
    client: SECClient,
    ticker: str,
    form: str,
    count: int = 5,
    *,
    cik_override: int | None = None,
) -> tuple[Company, list[Filing]]:
    """Return filings, loading referenced history only when recent data is short."""
    requested_form = validate_form(form)
    validate_count(count)
    company = resolve_company(client, ticker, cik_override=cik_override)
    payload = client.get_json(SUBMISSIONS_URL_TEMPLATE.format(cik=company.cik))
    if not isinstance(payload, Mapping):
        raise SECDataError("SEC submissions response must be a JSON object")
    submission_name = str(payload.get("name", "")).strip()
    if submission_name:
        company = Company(
            ticker=company.ticker,
            name=submission_name,
            cik=company.cik,
        )
    filings_object = payload.get("filings")
    if not isinstance(filings_object, Mapping):
        raise SECDataError("SEC submissions response is missing filings")
    recent = filings_object.get("recent")
    if not isinstance(recent, Mapping):
        raise SECDataError("SEC submissions response is missing filings.recent")
    discovered = parse_filing_arrays(
        company,
        recent,
        source="filings.recent",
        requested_form=requested_form,
    )
    selected = select_filings(discovered, requested_form, count)
    if len(selected) >= count:
        return company, selected

    for reference in parse_historical_file_references(filings_object):
        historical_payload = client.get_json(
            historical_submissions_url(reference.name)
        )
        if not isinstance(historical_payload, Mapping):
            raise SECDataError(
                f"SEC historical submissions file {reference.name} must be an object"
            )
        discovered.extend(
            parse_filing_arrays(
                company,
                historical_payload,
                source=reference.name,
                requested_form=requested_form,
            )
        )
        discovered = _deduplicate_filings(discovered)
        selected = select_filings(discovered, requested_form, count)
        if len(selected) >= count:
            break

    return company, selected
