"""One-shot Yahoo collection check using reviewed company inputs; no AWS calls."""

import argparse
import asyncio
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from zoneinfo import ZoneInfo

from finbot_ingestion.execution import BlockingExecution
from finbot_ingestion.repositories.dynamodb.companies import DynamoDBCompanyRepository
from finbot_ingestion.repositories.dynamodb.serialization import decode
from .contracts import CalendarSnapshot
from .provider import CalendarDataError, IncompleteCalendarSnapshot
from .providers.yahoo import YahooEarningsCalendarProvider
from .providers.yahoo_config import YahooConfig


def load_companies(path):
    """Read the existing CLI seed format without applying its write requests."""
    request = json.loads(Path(path).read_text())
    companies = []
    for action in request["TransactItems"]:
        if set(action) != {"Put"}:
            raise CalendarDataError("company input must contain only Put records")
        companies.append(DynamoDBCompanyRepository._validate(decode(action["Put"]["Item"])))
    if (not 1 <= len(companies) <= 1000 or any(not company.enabled for company in companies)
            or len({company.cik for company in companies}) != len(companies)
            or len({company.ticker for company in companies}) != len(companies)):
        raise CalendarDataError("company input must be nonempty, enabled and unambiguous")
    return tuple(companies)


async def fetch(companies, start, end, config):
    with BlockingExecution(max_workers=1, name="yahoo-check") as execution:
        provider = YahooEarningsCalendarProvider(config, execution)
        try:
            # On cancellation, stop admissions before waiting for the owned worker.
            return await asyncio.shield(provider.fetch_events(start, end, companies))
        finally:
            provider.stop_admissions()
            await provider.close()


def snapshot_report(snapshot, companies, start, end):
    expected_ciks = tuple(sorted(company.cik for company in companies))
    if (not isinstance(snapshot, CalendarSnapshot) or not snapshot.complete
            or snapshot.provider != "yahoo" or snapshot.start_date != start
            or snapshot.end_date != end or snapshot.company_ciks != expected_ciks):
        raise IncompleteCalendarSnapshot("collection differs from the requested scope")
    if len(snapshot.events) > 5000:
        raise CalendarDataError("collection exceeds the runtime event bound")
    by_cik = {company.cik: company for company in companies}
    events, seen = [], set()
    for event in snapshot.events:
        key = event.company_cik, event.expected_date
        if (event.company_cik not in by_cik or event.ticker != by_cik[event.company_cik].ticker
                or event.provider != "yahoo" or not start <= event.expected_date <= end
                or event.time_of_day not in (None, "unknown", "before_market", "after_market")
                or key in seen):
            raise CalendarDataError("invalid or duplicate calendar observation")
        seen.add(key)
        events.append({"ticker": event.ticker, "cik": event.company_cik,
            "expected_date": event.expected_date.isoformat(),
            "time_of_day": event.time_of_day or "unknown"})
    matched = {event["cik"] for event in events}
    return {"status": "collection_complete", "collection_complete": True,
        "event_count": len(events), "matched_companies": len(matched),
        "companies_without_observations": [company.ticker for company in companies if company.cik not in matched],
        "events": events}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check live Yahoo calendar collection without AWS writes or SEC calls")
    parser.add_argument("--companies-file", required=True, type=Path, help="reviewed DynamoDB company seed JSON; read only")
    parser.add_argument("--days", type=int, default=30, help="inclusive collection length (default 30; maximum 360)")
    parser.add_argument("--start-date", type=date.fromisoformat, help="default: today's America/New_York date")
    parser.add_argument("--output", required=True, type=Path, help="new local JSON report file; must not already exist")
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 360:
        parser.error("--days must be in [1, 360]")
    # Reserve the report before contacting Yahoo; never replace an input/report file.
    try:
        output = args.output.open("x", encoding="utf-8")
    except OSError as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        return 1
    started = time.monotonic()
    start = args.start_date or datetime.now(ZoneInfo("America/New_York")).date()
    end = start + timedelta(days=args.days - 1)
    report = {"report_schema_version": 1, "provider": "yahoo", "start_date": start.isoformat(),
        "end_date": end.isoformat(), "days_requested": args.days, "aws_writes": False,
        "scope_note": "Complete collection does not prove every company has a reported event or authorize activation."}
    code = 1
    with output:
        try:
            companies = load_companies(args.companies_file)
            report["requested_companies"] = len(companies)
            config = YahooConfig.from_env()
            report["bounds"] = {"max_fetch_seconds": config.max_fetch_seconds,
                "max_http_attempts": config.max_http_attempts, "max_raw_rows": config.max_raw_rows,
                "min_request_interval_seconds": config.min_request_interval_seconds}
            with TemporaryDirectory(prefix="finbot-calendar-check-") as cache:
                config = replace(config, cache_dir=cache)
                snapshot = asyncio.run(fetch(companies, start, end, config))
            report.update(snapshot_report(snapshot, companies, start, end))
            code = 0
        except (Exception, KeyboardInterrupt) as exc:
            # Provider errors can contain cookies or authenticated URLs; report type only.
            report.update(status="failed", collection_complete=False, error_type=type(exc).__name__)
        report["observed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        report["duration_seconds"] = round(time.monotonic() - started, 3)
        output.write(json.dumps(report, indent=2) + "\n")
    summary = {key: value for key, value in report.items() if key != "events"}
    print(json.dumps(summary, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
