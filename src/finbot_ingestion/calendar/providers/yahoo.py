"""Bounded day-slice collection and normalized Yahoo scheduling expectations."""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import threading
from zoneinfo import ZoneInfo

from finbot_ingestion.domain import ExpectedEarningsEvent
from ..contracts import CalendarSnapshot, date_range
from ..provider import CalendarDataError, IncompleteCalendarSnapshot
from .yahoo_client import FetchBudget, YahooPageClient, YahooStopped
from .yahoo_parser import parse_page

LOGGER = logging.getLogger(__name__)


class YahooEarningsCalendarProvider:
    name = "yahoo"

    def __init__(self, config, execution, *, client=None, market_timezone="America/New_York", now=None):
        if execution.max_workers != 1:
            raise ValueError("Yahoo requires one dedicated blocking worker")
        self.config, self.execution = config, execution
        self.client = client or YahooPageClient(config)
        self.zone = ZoneInfo(market_timezone)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.stop = threading.Event()
        self._lock = asyncio.Lock()
        self.metrics = None

    def stop_admissions(self):
        self.stop.set()

    async def close(self):
        self.stop_admissions()
        async with self._lock:
            await self.execution.call(self.client.close)

    def _enumerate(self, day, budget):
        total = schema = None
        observations = {}
        offset = 0
        for _ in range(self.config.max_pages_per_slice):
            budget.remaining()
            raw = self.client.page(day, self.config.page_size, offset, budget)
            page = parse_page(raw, day=day, size=self.config.page_size, offset=offset, zone=self.zone.key)
            budget.rows += len(page.observations)
            if budget.rows > self.config.max_raw_rows:
                raise IncompleteCalendarSnapshot("Yahoo raw row budget exhausted")
            if total is None:
                total, schema = page.total, page.schema
            if page.total != total or page.schema != schema:
                raise IncompleteCalendarSnapshot("Yahoo total/schema changed during enumeration")
            if len(observations) == total:
                if page.observations:
                    raise IncompleteCalendarSnapshot("Yahoo terminal page contains unexpected rows")
                return observations
            if not page.observations or len(page.observations) != min(self.config.page_size, total - offset):
                raise IncompleteCalendarSnapshot("Yahoo page cannot account for reported total")
            for observation in page.observations:
                if observation.key in observations:
                    raise IncompleteCalendarSnapshot("Yahoo duplicate/conflicting event across pages")
                observations[observation.key] = observation
            offset += len(page.observations)
        raise IncompleteCalendarSnapshot("Yahoo slice page bound exhausted")

    def _collect(self, start, end, companies):
        budget = FetchBudget(self.config, self.stop)
        source = {}
        for index in range((end - start).days + 1):
            day = start + timedelta(days=index)
            # One bounded restart of a slice; counts/deadline are never reset.
            for attempt in range(2):
                try:
                    first = self._enumerate(day, budget)
                    second = self._enumerate(day, budget)
                    if first != second:
                        raise IncompleteCalendarSnapshot("Yahoo verification pass changed event set")
                    break
                except IncompleteCalendarSnapshot:
                    if attempt == 1:
                        raise
                    budget.wait(1)
            for key, observation in first.items():
                if key in source and source[key] != observation:
                    raise IncompleteCalendarSnapshot("Yahoo adjacent slices conflict")
                source[key] = observation
        by_ticker = {company.ticker: company for company in companies}
        observed_at, events = self.now(), []
        for observation in source.values():
            day = observation.at.astimezone(self.zone).date()
            company = by_ticker.get(observation.ticker)
            if company is None or not start <= day <= end:
                continue
            events.append(ExpectedEarningsEvent(company_cik=company.cik, ticker=company.ticker,
                expected_date=day, synced_at=observed_at, provider=self.name,
                time_of_day={"BMO": "before_market", "AMC": "after_market"}.get(observation.timing, "unknown"),
                replacement_hint=observation.replacement_hint,
                raw_provider_payload={"title": observation.title, "timing": observation.timing,
                    "source_event_at": observation.at.isoformat()}))
        # A CIK/date key cannot represent two announcements. Validate before service writes.
        by_key = {}
        for event in events:
            identity = event.company_cik, event.expected_date
            if identity in by_key and by_key[identity] != event:
                raise IncompleteCalendarSnapshot("Yahoo conflicting announcements share a CIK/date")
            by_key[identity] = event
        events = tuple(by_key[key] for key in sorted(by_key))
        matched = len({event.company_cik for event in events})
        LOGGER.info("Yahoo collection completed", extra={"operation": "yahoo_calendar", "provider": self.name,
            "http_attempts": budget.attempts, "pages": budget.pages, "raw_rows": budget.rows,
            "requested_companies": len(companies), "matched_companies": matched,
            "missing_companies": len(companies) - matched, "event_count": len(events)})
        if self.metrics is not None:
            for name, count in (("YahooPages", budget.pages),
                                ("CalendarMatchedCompanies", matched), ("CalendarMissingCompanies", len(companies) - matched)):
                self.metrics.count(name, count)
            for timing in ("before_market", "after_market", "unknown"):
                self.metrics.count("CalendarTiming" + {"before_market": "Before", "after_market": "After", "unknown": "Unknown"}[timing],
                                   sum(event.time_of_day == timing for event in events))
        budget.remaining()
        return CalendarSnapshot(provider=self.name, start_date=start, end_date=end,
            company_ciks=tuple(company.cik for company in companies), events=events, complete=True)

    async def fetch_events(self, start_date, end_date, companies):
        date_range(start_date, end_date)
        companies = tuple(companies)
        if (not companies or any(not company.enabled for company in companies)
                or len({c.ticker for c in companies}) != len(companies)
                or len({c.cik for c in companies}) != len(companies)):
            raise CalendarDataError("Yahoo requires an unambiguous enabled company mapping")
        async with self._lock:
            try:
                if hasattr(self.client, "metrics"):
                    self.client.metrics = self.metrics
                return await self.execution.call(self._collect, start=start_date, end=end_date, companies=companies)
            except YahooStopped:
                raise asyncio.CancelledError from None
