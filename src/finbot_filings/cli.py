"""Command-line interface for SEC filing discovery and download."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import NoReturn, Sequence

from finbot_filings.acquisition import download_filing_bundle
from finbot_filings.config import (
    ConfigurationError,
    download_folder,
    sec_cik_overrides,
    section_folder,
    xbrl_folder,
)
from finbot_filings.models import Filing
from finbot_filings.parsing.batch import parse_downloaded_filings
from finbot_filings.sec.client import SECClient, SECError
from finbot_filings.sec.filings import discover_filings, validate_count, validate_form
from finbot_filings.storage.local import LocalFilingStorage
from finbot_filings.xbrl.download import download_xbrl_packages
from finbot_filings.xbrl.extract import extract_xbrl_filings, inspect_xbrl_filings
from finbot_filings.xbrl.query import show_xbrl_facts
from finbot_filings.xbrl.taxonomy import (
    extract_taxonomy_filings,
    inventory_taxonomy_filings,
)

LOGGER = logging.getLogger(__name__)
DEFAULT_COUNT = 5


def _positive_count(value: str) -> int:
    try:
        return validate_count(int(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _supported_form(value: str) -> str:
    try:
        return validate_form(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="finbot-filings",
        description="Discover and download public SEC 10-K and 10-Q filings.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("discover", "download"):
        command = subparsers.add_parser(name)
        command.add_argument("ticker")
        command.add_argument("--form", required=True, type=_supported_form)
        command.add_argument("--count", type=_positive_count, default=DEFAULT_COUNT)
        if name == "download":
            command.add_argument(
                "--download-folder",
                type=Path,
                help="override DOWNLOAD_FOLDER from the package config file",
            )
            command.add_argument("--overwrite", action="store_true")
    parse_command = subparsers.add_parser(
        "parse-sections",
        help="extract top-level sections using internal TOC anchors",
    )
    parse_command.add_argument(
        "--input-folder",
        type=Path,
        help="download root (default: DOWNLOAD_FOLDER)",
    )
    parse_command.add_argument(
        "--output-folder",
        type=Path,
        help="section output root (default: SECTION_FOLDER)",
    )
    parse_command.add_argument("--form", type=_supported_form)
    parse_command.add_argument("--overwrite", action="store_true")
    xbrl_command = subparsers.add_parser(
        "download-xbrl",
        help="download SEC XBRL packages and generated instances",
    )
    xbrl_command.add_argument("ticker", nargs="?")
    xbrl_command.add_argument(
        "--download-folder",
        type=Path,
        help="download root (default: DOWNLOAD_FOLDER)",
    )
    xbrl_command.add_argument("--form", type=_supported_form)
    xbrl_command.add_argument("--overwrite", action="store_true")
    inspect_command = subparsers.add_parser(
        "inspect-xbrl",
        help="validate and inventory downloaded XBRL inputs",
    )
    inspect_command.add_argument("ticker", nargs="?")
    inspect_command.add_argument("--form", type=_supported_form)
    inspect_command.add_argument("--download-folder", type=Path)
    extract_command = subparsers.add_parser(
        "extract-xbrl",
        help="normalize downloaded XBRL instances into Parquet facts",
    )
    extract_command.add_argument("ticker", nargs="?")
    extract_command.add_argument("--form", type=_supported_form)
    extract_command.add_argument("--download-folder", type=Path)
    extract_command.add_argument("--output-folder", type=Path)
    extract_command.add_argument("--overwrite", action="store_true")
    taxonomy_command = subparsers.add_parser(
        "inventory-taxonomy",
        help="inventory local taxonomy resources without network access",
    )
    taxonomy_command.add_argument("ticker", nargs="?")
    taxonomy_command.add_argument("--form", type=_supported_form)
    taxonomy_command.add_argument("--accession")
    taxonomy_command.add_argument("--download-folder", type=Path)
    taxonomy_command.add_argument("--output-folder", type=Path)
    taxonomy_command.add_argument("--overwrite", action="store_true")
    taxonomy_extract_command = subparsers.add_parser(
        "extract-taxonomy",
        help="materialize local XBRL labels and presentation relationships",
    )
    taxonomy_extract_command.add_argument("ticker", nargs="?")
    taxonomy_extract_command.add_argument("--form", type=_supported_form)
    taxonomy_extract_command.add_argument("--accession")
    taxonomy_extract_command.add_argument("--download-folder", type=Path)
    taxonomy_extract_command.add_argument("--output-folder", type=Path)
    taxonomy_extract_command.add_argument("--overwrite", action="store_true")
    show_command = subparsers.add_parser(
        "show-xbrl",
        help="display or export normalized XBRL facts",
    )
    show_command.add_argument("ticker")
    show_command.add_argument("--form", type=_supported_form)
    show_command.add_argument("--accession")
    show_command.add_argument("--concept")
    show_command.add_argument("--format", choices=("table", "json", "csv"), default="table")
    show_command.add_argument("--limit", type=_positive_count, default=50)
    show_command.add_argument("--output", type=Path)
    show_command.add_argument("--xbrl-folder", type=Path)
    return parser


def _format_period(filing: Filing) -> str:
    return filing.report_date.isoformat() if filing.report_date else "not reported"


def _print_discovery(company_name: str, ticker: str, filings: Sequence[Filing]) -> None:
    print(f"{ticker} — {company_name}")
    for filing in filings:
        print()
        print(
            f"{filing.form:<6} Filed: {filing.filing_date.isoformat()}   "
            f"Period: {_format_period(filing)}"
        )
        print(f"Accession: {filing.accession_number}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    try:
        if args.command == "parse-sections":
            summary = parse_downloaded_filings(
                input_root=args.input_folder or download_folder(),
                output_root=args.output_folder or section_folder(),
                form_type=args.form,
                overwrite=args.overwrite,
            )
            return 1 if summary.failed else 0

        if args.command == "download-xbrl":
            summary = download_xbrl_packages(
                client=SECClient(),
                download_root=args.download_folder or download_folder(),
                form_type=args.form,
                ticker=args.ticker,
                overwrite=args.overwrite,
            )
            return 1 if summary.failed else 0

        if args.command == "inspect-xbrl":
            inspected = inspect_xbrl_filings(
                download_root=args.download_folder or download_folder(),
                ticker=args.ticker,
                form_type=args.form,
            )
            if inspected == 0:
                print("No matching downloaded XBRL filings found.")
            return 0

        if args.command == "extract-xbrl":
            summary = extract_xbrl_filings(
                download_root=args.download_folder or download_folder(),
                output_root=args.output_folder or xbrl_folder(),
                ticker=args.ticker,
                form_type=args.form,
                overwrite=args.overwrite,
            )
            return 1 if summary.failed else 0

        if args.command == "inventory-taxonomy":
            summary = inventory_taxonomy_filings(
                download_root=args.download_folder or download_folder(),
                output_root=args.output_folder or xbrl_folder(),
                ticker=args.ticker,
                form_type=args.form,
                accession_number=args.accession,
                overwrite=args.overwrite,
            )
            return 1 if summary.failed else 0

        if args.command == "extract-taxonomy":
            summary = extract_taxonomy_filings(
                download_root=args.download_folder or download_folder(),
                output_root=args.output_folder or xbrl_folder(),
                ticker=args.ticker,
                form_type=args.form,
                accession_number=args.accession,
                overwrite=args.overwrite,
            )
            return 1 if summary.failed else 0

        if args.command == "show-xbrl":
            shown = show_xbrl_facts(
                xbrl_root=args.xbrl_folder or xbrl_folder(),
                ticker=args.ticker,
                form_type=args.form,
                accession_number=args.accession,
                concept=args.concept,
                output_format=args.format,
                limit=args.limit,
                output_path=args.output,
            )
            return 0 if shown else 1

        client = SECClient()
        ticker = args.ticker.strip().upper()
        company, filings = discover_filings(
            client,
            ticker,
            form=args.form,
            count=args.count,
            cik_override=sec_cik_overrides().get(ticker),
        )
        if not filings:
            LOGGER.error(
                "No exact %s filings found for %s after searching available SEC history.",
                args.form,
                ticker,
            )
            return 1
        if args.command == "discover":
            _print_discovery(company.name, company.ticker, filings)
            return 0

        storage = LocalFilingStorage(args.download_folder or download_folder())
        for filing in filings:
            result = download_filing_bundle(
                client=client,
                filing=filing,
                storage=storage,
                overwrite=args.overwrite,
            )
            if result.skipped:
                print("Skipped complete filing bundle:")
            else:
                print("Acquired filing bundle:")
            print(
                f"{filing.ticker} {filing.form} filed "
                f"{filing.filing_date.isoformat()}"
            )
            print(
                f"HTML: {result.document_acquisition_method} — "
                f"{result.paths.document}"
            )
            print(f"XBRL: {result.xbrl_status}")
        return 0
    except (ConfigurationError, SECError, OSError, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 1


def entrypoint() -> NoReturn:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
