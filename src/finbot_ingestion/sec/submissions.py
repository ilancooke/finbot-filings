"""Pure submissions parsing adapted from finbot_filings.sec.filings.

No ticker lookup, requests, history traversal, persistence, or count cutoff.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from finbot_ingestion.domain import Company, Filing
from finbot_ingestion.domain.identity import SUPPORTED_FORMS, normalize_cik
from finbot_ingestion.domain.validation import utc_datetime
from .urls import filing_index_url


from .errors import SECDataError


def parse_filing_arrays(
    company: Company, filing_arrays: Mapping[str, Any], *, discovered_at: datetime,
) -> list[Filing]:
    """Parse all relevant rows, deduplicate by accession, and sort newest first.

filed_at is the SEC acceptanceDateTime, not filingDate or reportDate.
Missing/naive acceptance timestamps fail explicitly rather than inventing time.
"""
    observed = utc_datetime(discovered_at, "discovered_at")
    if not isinstance(filing_arrays, Mapping):
        raise SECDataError("SEC filing arrays must be an object")
    columns: dict[str, list[Any]] = {}
    for name in ("accessionNumber", "form", "acceptanceDateTime"):
        value = filing_arrays.get(name)
        if not isinstance(value, list):
            raise SECDataError(f"SEC {name} must be an array")
        columns[name] = value
    length = len(columns["accessionNumber"])
    for name in ("primaryDocument", "reportDate", "filingDate"):
        if name in filing_arrays:
            value = filing_arrays[name]
            if not isinstance(value, list):
                raise SECDataError(f"SEC {name} must be an array when supplied")
            columns[name] = value
    if any(len(value) != length for value in columns.values()):
        raise SECDataError("SEC filing arrays have inconsistent lengths")

    unique: dict[str, Filing] = {}
    for index in range(length):
        raw_form = columns["form"][index]
        if not isinstance(raw_form, str):
            raise SECDataError(f"invalid SEC form at index {index}")
        form = raw_form.strip().upper()
        if form not in SUPPORTED_FORMS:
            continue
        try:
            raw_time = columns["acceptanceDateTime"][index]
            if not isinstance(raw_time, str) or "T" not in raw_time:
                raise ValueError("acceptanceDateTime must be an ISO timestamp with timezone")
            filed_at = utc_datetime(datetime.fromisoformat(raw_time), "acceptanceDateTime")
            primary = columns["primaryDocument"][index] if "primaryDocument" in columns else None
            if primary == "":
                primary = None
            accession = columns["accessionNumber"][index]
            filing = Filing(
                accession_number=accession, company_cik=company.cik,
                ticker=company.ticker, form_type=form, filed_at=filed_at,
                discovered_at=observed, filing_index_url=filing_index_url(company.cik, accession),
                primary_document_name=primary,
            )
            previous = unique.get(filing.accession_number)
            if previous is not None and previous != filing:
                raise ValueError("conflicting metadata for duplicate accession")
            unique.setdefault(filing.accession_number, filing)
        except (TypeError, ValueError) as exc:
            raise SECDataError(f"invalid SEC filing at index {index}: {exc}") from exc
    return sorted(unique.values(), key=lambda f: (f.filed_at, f.accession_number), reverse=True)


def parse_company_submissions(
    company: Company, payload: Mapping[str, Any], *, discovered_at: datetime,
) -> list[Filing]:
    """Adapt a company submissions response without exposing provider structures."""
    if not isinstance(payload, Mapping):
        raise SECDataError("SEC submissions response must be an object")
    if "cik" in payload:
        try:
            if normalize_cik(payload["cik"]) != company.cik:
                raise ValueError("response CIK does not match configured company")
        except ValueError as exc:
            raise SECDataError(str(exc)) from exc
    filings = payload.get("filings")
    if not isinstance(filings, Mapping) or not isinstance(filings.get("recent"), Mapping):
        raise SECDataError("SEC submissions response is missing filings.recent")
    return parse_filing_arrays(company, filings["recent"], discovered_at=discovered_at)


def parse_company_submissions_with_evidence(company, payload, *, discovered_at):
    """Optional malformed item evidence never suppresses otherwise valid ingestion.

    SEC's comma-separated exact item-code strings are the only supported encoding.
    Duplicate rows with conflicting evidence are ambiguous, not affirmative matches.
    """
    import re
    from finbot_ingestion.domain.sec_items import SECItemMetadata, FilingObservation
    from finbot_ingestion.domain.identity import normalize_accession_number

    filings = parse_company_submissions(company, payload, discovered_at=discovered_at)
    arrays = payload["filings"]["recent"]
    supplied = arrays.get("items")
    aligned = isinstance(supplied, list) and len(supplied) == len(arrays["accessionNumber"])
    by_accession = {}
    for index, raw in enumerate(arrays["accessionNumber"]):
        try:
            accession = normalize_accession_number(raw)
        except ValueError:
            continue
        status, items = "absent", ()
        if "items" in arrays and not aligned:
            status = "ambiguous"
        elif aligned:
            value = supplied[index]
            if value is None or value == "":
                pass
            elif not isinstance(value, str) or len(value) > 4096:
                status = "ambiguous"
            else:
                parts = tuple(part.strip() for part in value.split(","))
                if len(parts) <= 100 and all(re.fullmatch(r"[1-9]\.[0-9]{2}", part) for part in parts):
                    status, items = "known", tuple(sorted(set(parts)))
                else:
                    status = "ambiguous"
        evidence = SECItemMetadata(accession, company.cik, status, items)
        if accession in by_accession and by_accession[accession] != evidence:
            evidence = SECItemMetadata(accession, company.cik, "ambiguous")
        by_accession[accession] = evidence
    return tuple(FilingObservation(f, by_accession[f.accession_number]) for f in filings)
