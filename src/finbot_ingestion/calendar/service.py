"""Explicit, bounded calendar refreshes. Runtime scheduling belongs to Phase 6."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, time, timedelta, timezone
import logging
import json
from uuid import uuid4
from zoneinfo import ZoneInfo

from finbot_ingestion.domain.validation import utc_datetime
from finbot_ingestion.ingestion.retry_policy import RetryPolicy
from finbot_ingestion.ingestion.work_control import retryable
from .config import CalendarConfig
from .contracts import CalendarHealth, CalendarSnapshot, CalendarSyncRun, CalendarSyncSuccess, date_range
from .provider import CalendarDataError, CalendarTransientError, IncompleteCalendarSnapshot
from .providers import PlaceholderCalendarProvider
from .reconciliation import AuthoritativeSnapshotPolicy, ReplacementOnlyPolicy

LOGGER = logging.getLogger(__name__)


class CalendarSyncService:
    """Share one instance/lock in one event loop; no distributed task coordination."""

    def __init__(self, provider, companies, calendar, config=None, *, now=None,
                 sleeper=asyncio.sleep, random_value=None, reconciliation=None,
                 market_timezone="UTC", reconciliation_lookback_days=0):
        self.config = config or CalendarConfig()
        if provider.name != self.config.provider:
            raise ValueError("provider adapter and configuration disagree")
        self.provider, self.companies, self.calendar = provider, companies, calendar
        if provider.name == "yahoo" and not isinstance(reconciliation, ReplacementOnlyPolicy):
            raise ValueError("Yahoo requires replacement-only reconciliation")
        self.reconciliation = reconciliation or AuthoritativeSnapshotPolicy()
        self.zone = ZoneInfo(market_timezone)
        if type(reconciliation_lookback_days) is not int or reconciliation_lookback_days < 0:
            raise ValueError("reconciliation lookback must be a nonnegative integer")
        self.reconciliation_lookback_days = reconciliation_lookback_days
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.sleep, self.random_value = sleeper, random_value
        self._lock = asyncio.Lock()
        self.metrics = None

    @classmethod
    def with_placeholder(cls, companies, calendar, config=None, **options):
        return cls(PlaceholderCalendarProvider(), companies, calendar, config, **options)

    async def _retry(self, action, *, attempts, operation):
        policy = RetryPolicy(attempts, self.config.backoff_base_seconds, self.config.backoff_cap_seconds)
        for attempt in range(1, attempts + 1):
            try:
                result = await action()
                if attempt > 1:
                    LOGGER.info("Calendar operation recovered", extra={"operation": operation,
                        "provider": self.provider.name, "attempt_number": attempt})
                return result
            except Exception as exc:
                will_retry = (isinstance(exc, (CalendarTransientError, IncompleteCalendarSnapshot))
                              or retryable(exc)) and attempt < attempts
                # Never echo provider exceptions, which may contain credential-bearing URLs.
                LOGGER.warning("Calendar operation failed", extra={"operation": operation,
                    "provider": self.provider.name, "attempt_number": attempt,
                    "error_type": type(exc).__name__, "will_retry": will_retry})
                if not will_retry:
                    raise
                if self.metrics is not None:
                    self.metrics.count("RetryAttempts")
                options = {"random_value": self.random_value} if self.random_value is not None else {}
                await self.sleep(policy.delay(attempt, **options))

    async def _checkpoint(self, action, operation):
        return await self._retry(action, attempts=self.config.checkpoint_attempts, operation=operation)

    async def _companies(self):
        companies, token = {}, None
        while True:
            page = await self._checkpoint(lambda: self.companies.list_enabled(
                page_size=self.config.company_page_size, token=token), "calendar_companies")
            for company in page.items:
                if not company.enabled:
                    continue
                if company.cik in companies and companies[company.cik] != company:
                    raise CalendarDataError("conflicting curated company records")
                companies[company.cik] = company
                if len(companies) > self.config.max_companies:
                    raise CalendarDataError("curated company count exceeds configured bound")
            token = page.next_token
            if token is None:
                break
        # Symbol-only providers must not guess which stable identity a duplicate ticker means.
        if len({company.ticker for company in companies.values()}) != len(companies):
            raise CalendarDataError("curated tickers map to multiple CIKs")
        return tuple(companies[key] for key in sorted(companies))

    def _normalize(self, snapshot, run, companies):
        if not isinstance(snapshot, CalendarSnapshot):
            raise CalendarDataError("provider must return CalendarSnapshot")
        if not snapshot.complete:
            raise IncompleteCalendarSnapshot("provider snapshot is incomplete")
        if (snapshot.provider != run.provider or snapshot.start_date != run.start_date
                or snapshot.end_date != run.end_date or snapshot.company_ciks != run.company_ciks):
            raise IncompleteCalendarSnapshot("provider coverage differs from requested scope")
        if len(snapshot.events) > self.config.max_snapshot_events:
            raise CalendarDataError("calendar snapshot exceeds configured bound")
        by_cik = {company.cik: company for company in companies}
        events, identities = {}, {}
        for supplied in snapshot.events:
            if (supplied.company_cik not in by_cik or supplied.provider != run.provider
                    or not run.start_date <= supplied.expected_date <= run.end_date):
                raise CalendarDataError("event is outside confirmed provider/company/date scope")
            if supplied.time_of_day not in (None, "unknown", "before_market", "after_market"):
                raise CalendarDataError("provider did not normalize earnings time")
            event = replace(supplied, ticker=by_cik[supplied.company_cik].ticker,
                synced_at=run.observed_at, time_of_day=supplied.time_of_day or "unknown",
                raw_provider_payload=deepcopy(supplied.raw_provider_payload))
            key = (event.expected_date, event.company_cik)
            if key in events and events[key] != event:
                raise CalendarDataError("conflicting duplicate calendar expectation")
            if event.provider_event_id is not None:
                if not isinstance(event.provider_event_id, str) or not event.provider_event_id:
                    raise CalendarDataError("provider event ID must be nonempty text")
                identity = (event.company_cik, event.provider_event_id)
                if identity in identities and identities[identity] != key:
                    raise CalendarDataError("one provider event occupies conflicting dates")
                identities[identity] = key
            if event.raw_provider_payload is not None:
                if not isinstance(event.raw_provider_payload, dict):
                    raise CalendarDataError("raw diagnostic payload must be a JSON object")
                try:
                    json.dumps(event.raw_provider_payload, allow_nan=False)
                except (ValueError, TypeError) as exc:
                    raise CalendarDataError("raw diagnostic payload must contain finite JSON values") from exc
            events[key] = event
        return tuple(events[key] for key in sorted(events))

    async def sync_once(self, start_date, end_date, *, kind="full"):
        date_range(start_date, end_date)
        if (end_date - start_date).days + 1 > self.config.lookahead_days:
            raise ValueError("requested calendar span exceeds configured lookahead")
        async with self._lock:
            companies = await self._companies()
            if not companies:
                raise CalendarDataError("no enabled curated companies; sync cannot establish coverage")
            requested = CalendarSyncRun(request_id=uuid4().hex, provider=self.provider.name, kind=kind,
                start_date=start_date, end_date=end_date, company_ciks=tuple(company.cik for company in companies),
                observed_at=utc_datetime(self.now(), "now"))
            run = await self._checkpoint(lambda: self.calendar.begin_sync(requested), "calendar_begin")
            try:
                async def fetch():
                    snapshot = await self.provider.fetch_events(start_date, end_date, companies)
                    return self._normalize(snapshot, run, companies)
                events = await self._retry(fetch, attempts=self.config.provider_attempts, operation="calendar_fetch")
                read_start = start_date - timedelta(days=self.reconciliation_lookback_days)
                start = datetime.combine(read_start, time.min, timezone.utc)
                end = datetime.combine(end_date, time.min, timezone.utc)
                previous = await self._checkpoint(lambda: self.calendar.get_events(start, end), "calendar_read")
                incoming = {(event.expected_date, event.company_cik): event for event in events}
                existing = {(event.expected_date, event.company_cik): event for event in previous}
                if any(key in existing and existing[key].provider != run.provider for key in incoming):
                    raise CalendarDataError("another provider owns an overlapping expectation")
                if isinstance(self.reconciliation, ReplacementOnlyPolicy) and any(
                        key in existing and existing[key].replacement_hint is not None
                        and value.replacement_hint is not None
                        and existing[key].replacement_hint != value.replacement_hint
                        for key, value in incoming.items()):
                    raise IncompleteCalendarSnapshot("different announcements occupy the same calendar key")
                scoped = tuple(event for event in previous if event.provider == run.provider
                    and event.company_cik in run.company_ciks)
                plan = self.reconciliation.plan(scoped, events)
                await self._checkpoint(lambda: self.calendar.upsert_events(events), "calendar_upsert")
                cancelled = []
                for pair in plan.replacements:
                    canonical = await self._checkpoint(lambda pair=pair: self.calendar.get_event(
                        pair.incoming.expected_date, pair.incoming.company_cik), "calendar_confirm_replacement")
                    if canonical != pair.incoming:
                        raise IncompleteCalendarSnapshot("replacement observation was not confirmed durable")
                    applied = await self._checkpoint(lambda pair=pair: self.calendar.cancel_event(
                        pair.previous, at=run.observed_at, require_unchanged=True), "calendar_replace")
                    if applied:
                        cancelled.append(pair.previous)
                for event in plan.cancellations:
                    applied = await self._checkpoint(lambda event=event: self.calendar.cancel_event(
                        event, at=run.observed_at), "calendar_cancel")
                    if applied:
                        cancelled.append(event)
                success = CalendarSyncSuccess(run=run,
                    completed_at=max(utc_datetime(self.now(), "now"), run.observed_at),
                    event_count=len(events), cancelled_count=len(cancelled))
                await self._checkpoint(lambda: self.calendar.complete_sync(success), "calendar_complete")
            except Exception as exc:
                # A checkpoint outage must not hide the original failed refresh.
                at = max(utc_datetime(self.now(), "now"), run.observed_at)
                try:
                    await self._checkpoint(lambda: self.calendar.fail_sync(run,
                        error=type(exc).__name__, at=at), "calendar_failure")
                except Exception as checkpoint_error:
                    LOGGER.warning("Calendar failure checkpoint unavailable", extra={"provider": run.provider,
                        "error_type": type(checkpoint_error).__name__})
                raise
            self._log_changes(previous, events, cancelled, run)
            if self.metrics is not None:
                for name, count in (("CalendarPreservedOmissions", plan.preserved_count),
                                    ("CalendarAmbiguousReplacements", plan.ambiguous_count),
                                    ("CalendarReplacements", len(cancelled) if plan.replacements else 0)):
                    self.metrics.count(name, count)
            LOGGER.info("Calendar sync completed", extra={"operation": "calendar_sync", "provider": run.provider,
                "sync_kind": kind, "event_count": len(events), "cancelled_count": len(cancelled),
                "preserved_count": plan.preserved_count, "ambiguous_count": plan.ambiguous_count,
                "company_count": len(companies), "start_date": start_date.isoformat(), "end_date": end_date.isoformat()})
            return success

    def _log_changes(self, previous, events, obsolete, run):
        old = {(event.company_cik, event.expected_date): event for event in previous}
        identities = {(event.company_cik, event.provider_event_id): event for event in previous
                      if event.provider == run.provider and event.provider_event_id is not None}
        for event in events:
            before = old.get((event.company_cik, event.expected_date))
            matched = identities.get((event.company_cik, event.provider_event_id)) if event.provider_event_id else None
            change = ("date_changed" if matched and matched.expected_date != event.expected_date else
                      "added" if before is None else "time_changed" if before.time_of_day != event.time_of_day else None)
            if change:
                LOGGER.info("Calendar expectation changed", extra={"operation": "calendar_change", "provider": run.provider,
                    "cik": event.company_cik, "ticker": event.ticker, "change": change,
                    "expected_date": event.expected_date.isoformat(), "time_of_day": event.time_of_day,
                    "previous_date": matched.expected_date.isoformat() if matched else None,
                    "previous_time_of_day": before.time_of_day if before else None})
        for event in obsolete:
            LOGGER.info("Calendar expectation changed", extra={"operation": "calendar_change", "provider": run.provider,
                "cik": event.company_cik, "ticker": event.ticker, "change": "removed",
                "expected_date": event.expected_date.isoformat()})

    async def sync_full(self):
        start = self.today()
        return await self.sync_once(start, start + timedelta(days=self.config.lookahead_days - 1), kind="full")

    async def sync_near_term(self):
        start = self.today()
        return await self.sync_once(start, start + timedelta(days=self.config.near_term_days - 1), kind="near_term")

    def today(self):
        return utc_datetime(self.now(), "now").astimezone(self.zone).date()

    async def health(self):
        state = await self._checkpoint(lambda: self.calendar.get_sync_state(self.provider.name), "calendar_health")
        age = None if state is None or state.full_success is None else max(0.0,
            (utc_datetime(self.now(), "now") - state.full_success.completed_at).total_seconds())
        return CalendarHealth(state, age, age is None or age >= self.config.stale_after_seconds)
