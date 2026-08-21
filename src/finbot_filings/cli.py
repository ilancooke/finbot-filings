"""Command-line interface for SEC filing discovery and download."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import NoReturn, Sequence

from finbot_filings.config import ConfigurationError, download_folder
from finbot_filings.models import Filing
from finbot_filings.sec.client import SECClient, SECError
from finbot_filings.sec.filings import discover_filings, validate_count, validate_form
from finbot_filings.storage.local import LocalFilingStorage

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
        client = SECClient()
        company, filings = discover_filings(
            client, args.ticker, form=args.form, count=args.count
        )
        if args.command == "discover":
            _print_discovery(company.name, company.ticker, filings)
            return 0

        storage = LocalFilingStorage(args.download_folder or download_folder())
        for filing in filings:
            if storage.exists(filing) and not args.overwrite:
                print("Skipped existing:")
                print(
                    f"{filing.ticker} {filing.form} filed "
                    f"{filing.filing_date.isoformat()}"
                )
                continue
            document = client.get_bytes(filing.document_url)
            result = storage.store(filing, document, overwrite=args.overwrite)
            print("Downloaded:")
            print(
                f"{filing.ticker} {filing.form} filed "
                f"{filing.filing_date.isoformat()}"
            )
            print(result.paths.document)
        return 0
    except (ConfigurationError, SECError, OSError) as exc:
        LOGGER.error("%s", exc)
        return 1


def entrypoint() -> NoReturn:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()
