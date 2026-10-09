"""Supervised single-process application over injectable, offline-testable services."""

import asyncio
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, time, timedelta, timezone
import logging

from finbot_ingestion.calendar.provider import (
    CalendarProviderNotConfigured, CalendarTransientError, IncompleteCalendarSnapshot,
)
from finbot_ingestion.domain.satisfaction import EventIdentity, EventSatisfaction
from finbot_ingestion.ingestion.work_queue import WorkQueue
from finbot_ingestion.ingestion.work_control import work_error, retryable
from finbot_ingestion.observability.health import HealthFile
from finbot_ingestion.observability.metrics import Metrics
from finbot_ingestion.scheduler.earnings_satisfaction_policy import EarningsSatisfactionPolicy
from finbot_ingestion.scheduler.polling_scheduler import PollingScheduler
from .clock import Clock

LOGGER = logging.getLogger(__name__)


class RuntimeApplication:
    def __init__(self, *, config, calendar_service, satisfaction, worker, recovery, windows,
                 clock=None, metrics=None, health_file=None):
        self.config, self.calendar_service, self.satisfaction = config, calendar_service, satisfaction
        if not 1 <= worker.control.config.max_inflight_artifacts <= 16:
            raise ValueError("runtime artifact worker count must be in [1, 16]")
        self.worker, self.recovery, self.windows = worker, recovery, windows
        self.clock, self.metrics = clock or Clock(), metrics or Metrics()
        self.health_file = health_file or HealthFile(config.health_path)
        self.polls = WorkQueue(config.company_queue_size, self.clock)
        self.filings = WorkQueue(config.filing_queue_size, self.clock)
        self.artifacts = WorkQueue(config.artifact_queue_size, self.clock)
        self.scheduler = PollingScheduler(config, self.clock, windows, self.polls)
        self.policy = EarningsSatisfactionPolicy()
        self.stop = asyncio.Event()
        self.reload_requested = asyncio.Event()
        self._state_lock = asyncio.Lock()
        self.tasks, self.activity = {}, {}
        self.calendar_health = None
        self.scope_matches = False
        self.provider_configured = calendar_service.provider.name != "placeholder"
        self.last_recovery = None
        self.recovery_summary = None
        self.startup_recovery_complete = False
        self.draining = False
        self.worker.metrics = self.worker.discovery.metrics = self.metrics
        self.worker.control.metrics = self.metrics
        self.calendar_service.metrics = self.metrics
        if hasattr(self.worker.discovery.sec, "metrics"):
            self.worker.discovery.sec.metrics = self.metrics
        self.recovery.progress = lambda: self.progress("recovery")

    def request_stop(self):
        self.polls.accepting = False
        self.stop.set()

    def progress(self, name):
        self.activity[name] = (self.clock.monotonic(), self.activity.get(name, (0, False))[1])

    @contextmanager
    def busy(self, name):
        self.activity[name] = (self.clock.monotonic(), True)
        try:
            yield
        finally:
            self.activity[name] = (self.clock.monotonic(), False)

    async def reload(self):
        async with self._state_lock:
            companies = await self.calendar_service._companies()
            if not companies:
                raise ValueError("runtime needs a nonempty enabled company universe")
            c = self.calendar_service.config
            today = self.clock.now().astimezone(self.windows.zone).date()
            start = today - timedelta(days=self.windows.lookback_days)
            end = today + timedelta(days=max(c.near_term_days, self.windows.forward_days) - 1)
            # Repository selects inclusive UTC DATE partitions, not market instants.
            events = await self.calendar_service.calendar.get_events(
                datetime.combine(start, time.min, timezone.utc), datetime.combine(end, time.min, timezone.utc))
            ciks = {company.cik for company in companies}
            events = tuple(e for e in events if e.company_cik in ciks and e.provider == c.provider)
            if len(events) > c.max_snapshot_events:
                raise ValueError("loaded calendar exceeds configured bound")
            satisfied = set()
            for event in events:
                # Validate sessions and all event identities before replacing the schedule.
                self.windows.window(event)
                identity = EventIdentity.from_event(event)
                checkpoint = await self.satisfaction.get(identity)
                if checkpoint is not None:
                    satisfied.add(identity)
            health = await self.calendar_service.health()
            state = health.state
            success = state.full_success if state is not None else None
            utc_today = self.clock.now().date()
            self.scope_matches = success is not None and (
                success.run.company_ciks == tuple(sorted(ciks)) and success.run.start_date <= utc_today
                and success.run.end_date >= utc_today + timedelta(days=c.lookahead_days - 1))
            self.calendar_health = health
            self.scheduler.reload(companies, events, satisfied)

    async def satisfy(self, observations):
        async with self._state_lock:
            events = [e for e in self.scheduler.events if EventIdentity.from_event(e) not in self.scheduler.satisfied]
            matches = []
            for event in events:
                window = self.windows.window(event)
                selected = self.policy.select(event, window, observations)
                if selected is not None:
                    observation, decision = selected
                    matches.append((event, window, observation, decision))
                else:
                    for observation in observations:
                        if observation.filing.company_cik == event.company_cik and window.contains(observation.filing.filed_at):
                            decision = self.policy.evaluate(event, window, observation.filing, observation.sec_items)
                            LOGGER.info("Earnings polling expectation remains unsatisfied", extra={
                                "operation": "earnings_satisfaction", "cik": event.company_cik,
                                "accession_number": observation.filing.accession_number,
                                "reason": decision.reason, "policy_version": decision.policy_version})
            for event, window, observation, decision in matches:
                # One source filing must not suppress two overlapping expectations.
                if any(other is not event and other.company_cik == event.company_cik
                       and self.windows.window(other).contains(observation.filing.filed_at) for other in events):
                    LOGGER.warning("Ambiguous overlapping earnings expectations", extra={
                        "operation": "earnings_satisfaction", "cik": event.company_cik,
                        "reason": "overlapping_expectations", "policy_version": decision.policy_version})
                    continue
                value = EventSatisfaction.from_match(event, window, observation, decision, at=self.clock.now())
                await self.worker.control.checkpoint(lambda: self.satisfaction.create_if_absent(value),
                    operation="earnings_satisfaction", identity=value.identity.key)
                self.scheduler.satisfied.add(value.identity)
                self.metrics.count("EarningsExpectationsSatisfied")
                LOGGER.info("Earnings polling expectation satisfied by scheduling heuristic", extra={
                    "operation": "earnings_satisfaction", "cik": event.company_cik,
                    "accession_number": value.matched_accession_number,
                    "reason": value.match_reason, "policy_version": value.match_policy_version})

    async def scheduler_loop(self):
        while True:
            self.scheduler.tick()
            self.progress("scheduler")
            await self.clock.sleep(self.config.tick_seconds)

    async def poll_loop(self, name):
        while True:
            item = await self.polls.get()
            retry_delay = None
            try:
                with self.busy(name):
                    state = self.scheduler.states.get(item.key)
                    if state is None:  # Disabled/removed after queue admission.
                        continue
                    self.metrics.observe("PollQueueDelayMs", (self.clock.monotonic() - item.queued_at) * 1000)
                    observations = await self.worker.discovery.discover_with_evidence(state.company)
                    # Preserve all filings for ingestion even if matching/checkpointing fails.
                    for observation in observations:
                        checkpoint = await self.worker.filings.get_checkpoint(observation.filing.accession_number)
                        if checkpoint is not None and checkpoint.enumeration_completed_at is None:
                            await self.filings.put(observation.filing.accession_number, observation.filing.accession_number)
                    await self.satisfy(observations)
            except Exception as exc:
                if not work_error(exc):
                    raise
                LOGGER.warning("Company polling failed", extra={"operation": "company_poll", "cik": item.key,
                    "error_type": type(exc).__name__, "will_retry": True})
                state = self.scheduler.states.get(item.key)
                retry_delay = self.worker.control.policy.delay(min((state.failures if state else 0) + 1,
                    self.worker.control.config.max_stage_failures))
            finally:
                self.scheduler.completed(item.key, retry_delay=retry_delay)
                self.polls.done(item)

    async def enumeration_loop(self, name):
        while True:
            item = await self.filings.get()
            try:
                with self.busy(name):
                    self.metrics.observe("FilingQueueDelayMs", (self.clock.monotonic() - item.queued_at) * 1000)
                    result = await self.worker.enumerate_only(item.key)
                    for identity in result.artifact_ids:
                        await self.artifacts.put(identity, identity)
            except Exception as exc:
                if not work_error(exc):
                    raise
                LOGGER.warning("Enumeration dispatch failed; durable recovery remains pending", extra={
                    "operation": "enumeration_dispatch", "accession_number": item.key, "error_type": type(exc).__name__})
            finally:
                self.filings.done(item)

    async def artifact_loop(self, name):
        while True:
            item = await self.artifacts.get()
            try:
                with self.busy(name):
                    self.metrics.observe("ArtifactQueueDelayMs", (self.clock.monotonic() - item.queued_at) * 1000)
                    await self.worker.process_artifact(item.key)
            except Exception as exc:
                if not work_error(exc):
                    raise
                LOGGER.warning("Artifact dispatch failed; durable recovery remains pending", extra={
                    "operation": "artifact_dispatch", "artifact_id": item.key, "error_type": type(exc).__name__})
            finally:
                self.artifacts.done(item)

    async def recovery_loop(self):
        while True:
            try:
                with self.busy("recovery"):
                    summary = await self.recovery.run_pass()
                    self.recovery_summary = asdict(summary)
                    self.last_recovery = self.clock.now()
                    self.startup_recovery_complete = True
                    self.metrics.count("RecoveryErrors", summary.errors)
            except Exception as exc:
                if not retryable(exc):
                    raise
                LOGGER.warning("Recovery pass failed", extra={"operation": "recovery", "error_type": type(exc).__name__})
            await self.clock.sleep(self.config.recovery_seconds)

    async def reload_loop(self):
        while True:
            # Event wait uses real async timeout; fake tests can request an immediate reload.
            try:
                await asyncio.wait_for(self.reload_requested.wait(), self.config.reload_seconds)
            except TimeoutError:
                pass
            self.reload_requested.clear()
            try:
                with self.busy("reload"):
                    await self.reload()
            except Exception as exc:
                if not retryable(exc):
                    raise
                LOGGER.warning("Runtime state reload failed", extra={"operation": "reload", "error_type": type(exc).__name__})

    async def refresh_loop(self):
        c = self.calendar_service.config
        full_due, near_due = 0.0, 0.0
        if self.calendar_health is not None and self.calendar_health.state is not None:
            for kind, cadence in (("full", c.full_refresh_seconds), ("near_term", c.near_term_refresh_seconds)):
                success = getattr(self.calendar_health.state, kind + "_success")
                if success is not None:
                    remaining = max(0, cadence - (self.clock.now() - success.completed_at).total_seconds())
                    if kind == "full":
                        full_due = self.clock.monotonic() + (remaining if self.scope_matches else 0)
                    else:
                        near_due = self.clock.monotonic() + remaining
        while True:
            now = self.clock.monotonic()
            kind = "full" if now >= full_due else "near_term" if c.near_term_refresh_seconds and now >= near_due else None
            if kind is not None:
                try:
                    with self.busy("refresh"):
                        await (self.calendar_service.sync_full() if kind == "full" else self.calendar_service.sync_near_term())
                    due = self.clock.monotonic()
                    if kind == "full":
                        full_due = due + c.full_refresh_seconds
                    near_due = due + c.near_term_refresh_seconds
                    self.reload_requested.set()
                except Exception as exc:
                    if not isinstance(exc, (CalendarProviderNotConfigured, CalendarTransientError, IncompleteCalendarSnapshot)) and not retryable(exc):
                        raise
                    LOGGER.warning("Calendar refresh unavailable", extra={"operation": "calendar_refresh",
                        "provider": self.calendar_service.provider.name, "error_type": type(exc).__name__})
                    due = self.clock.monotonic() + self.config.refresh_retry_seconds
                    if kind == "full":
                        full_due = due
                    near_due = due  # Do not hammer an unavailable provider with the other scope.
            await self.clock.sleep(min(self.config.tick_seconds, self.config.refresh_retry_seconds))

    def health_snapshot(self):
        statuses = {}
        for name, task in self.tasks.items():
            at, busy = self.activity.get(name, (self.clock.monotonic(), False))
            statuses[name] = {"running": not task.done(), "busy": busy,
                "stalled": busy and self.clock.monotonic() - at > self.config.stall_seconds}
        live = bool(statuses) and all(v["running"] and not v["stalled"] for v in statuses.values())
        health = self.calendar_health
        age = None
        if health is not None and health.state is not None and health.state.full_success is not None:
            age = max(0, (self.clock.now() - health.state.full_success.completed_at).total_seconds())
        calendar_stale = age is None or age >= self.calendar_service.config.stale_after_seconds or not self.scope_matches
        return {"heartbeat_at": self.clock.now().isoformat(), "live": live and not self.draining,
            "draining": self.draining, "tasks": statuses, "calendar_stale": calendar_stale,
            "calendar_full_sync_age_seconds": age, "calendar_scope_matches": self.scope_matches,
            "provider_configured": self.provider_configured, "startup_recovery_complete": self.startup_recovery_complete,
            "last_recovery_at": self.last_recovery.isoformat() if self.last_recovery else None,
            "recovery": self.recovery_summary, "queues": {"polls": self.polls.snapshot(),
                "filings": self.filings.snapshot(), "artifacts": self.artifacts.snapshot()}}

    async def heartbeat_loop(self):
        while True:
            self.progress("heartbeat")
            value = self.health_snapshot()
            self.health_file.write(value)
            self.metrics.observe("RuntimeHealthy", int(value["live"]), unit="Count")
            self.metrics.observe("CalendarScopeMatches", int(self.scope_matches), unit="Count")
            self.metrics.observe("CalendarStale", int(value["calendar_stale"]), unit="Count")
            self.metrics.observe("CalendarProviderConfigured", int(self.provider_configured), unit="Count")
            if value["calendar_full_sync_age_seconds"] is not None:
                self.metrics.observe("CalendarSyncAgeSeconds", value["calendar_full_sync_age_seconds"], unit="Seconds")
            if any(status["stalled"] for status in value["tasks"].values()):
                raise RuntimeError("required runtime task stalled")
            await self.clock.sleep(self.config.heartbeat_seconds)

    async def metrics_loop(self):
        while True:
            self.progress("metrics")
            self.metrics.observe("ActiveCompanies", self.scheduler.active_count, unit="Count")
            for name, queue in (("Poll", self.polls), ("Filing", self.filings), ("Artifact", self.artifacts)):
                self.metrics.observe(name + "QueueDepth", queue.queue.qsize(), unit="Count")
            self.metrics.flush()
            await self.clock.sleep(self.config.metrics_flush_seconds)

    async def run(self):
        stop_waiter = None
        try:
            await self.reload()
            if self.stop.is_set():
                return
            loops = {"recovery": self.recovery_loop(), "refresh": self.refresh_loop(),
                "reload": self.reload_loop(), "scheduler": self.scheduler_loop(),
                "heartbeat": self.heartbeat_loop(), "metrics": self.metrics_loop()}
            loops.update({f"poll-{i}": self.poll_loop(f"poll-{i}") for i in range(self.config.poll_workers)})
            loops.update({f"enumeration-{i}": self.enumeration_loop(f"enumeration-{i}") for i in range(self.config.enumeration_workers)})
            loops.update({f"artifact-{i}": self.artifact_loop(f"artifact-{i}")
                          for i in range(self.worker.control.config.max_inflight_artifacts)})
            self.tasks = {name: asyncio.create_task(loop, name=name) for name, loop in loops.items()}
            stop_waiter = asyncio.create_task(self.stop.wait())
            done, _ = await asyncio.wait([stop_waiter, *self.tasks.values()], return_when=asyncio.FIRST_COMPLETED)
            for name, task in self.tasks.items():
                if task in done:
                    LOGGER.error("Required runtime task stopped", extra={"task": name})
                    task.result()
                    raise RuntimeError(f"required task returned unexpectedly: {name}")
        finally:
            cleanup = asyncio.create_task(self.shutdown(stop_waiter))
            cancelled = False
            while True:
                try:
                    await asyncio.shield(cleanup)
                    break
                except asyncio.CancelledError:
                    if cleanup.cancelled():
                        raise
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError

    async def shutdown(self, stop_waiter=None):
        self.draining = True
        self.polls.accepting = False
        producers = [task for name, task in self.tasks.items()
                     if name in ("scheduler", "reload", "refresh", "recovery")]
        for task in producers:
            task.cancel()
        await asyncio.gather(*producers, return_exceptions=True)
        workers = [task for name, task in self.tasks.items() if name.startswith(("poll-", "enumeration-", "artifact-"))]
        if workers and all(not task.done() for task in workers):
            try:
                async def drain():
                    await self.polls.queue.join()
                    await self.filings.queue.join()
                    await self.artifacts.queue.join()
                await asyncio.wait_for(drain(), self.config.shutdown_grace_seconds)
            except TimeoutError:
                LOGGER.warning("Shutdown drain grace expired; unfinished work remains recoverable")
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        if stop_waiter is not None:
            stop_waiter.cancel()
            await asyncio.gather(stop_waiter, return_exceptions=True)
        for queue in (self.polls, self.filings, self.artifacts):
            queue.discard()
        try:
            self.metrics.flush()
        finally:
            self.health_file.remove()
