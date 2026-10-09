"""500-company fake-clock runtime replay through the real SEC client/shared limiter.

All SDK calls use the stateful Phase 4 fake; no measured figure is a live SLA.
"""

import asyncio
from datetime import date, timedelta
import heapq
import json
from pathlib import Path
import threading
import time

import pytest

from test_dynamodb_repositories import db, NOW, ACCESSION, make_filing, make_artifact
from test_phase4 import system
from finbot_ingestion.calendar import CalendarConfig
from finbot_ingestion.calendar.service import CalendarSyncService
from finbot_ingestion.domain import Company, ExpectedEarningsEvent
from finbot_ingestion.repositories.package_checkpoint import PackageCheckpoint
from finbot_ingestion.sec.client import DownloadedDocument
from finbot_ingestion.sec.urls import filing_index_url, document_url
from finbot_ingestion.ingestion.recovery_service import RecoveryService
from finbot_ingestion.observability.metrics import Metrics
from finbot_ingestion.repositories.dynamodb.satisfaction import DynamoDBSatisfactionRepository
from finbot_ingestion.runtime.application import RuntimeApplication
from finbot_ingestion.runtime.config import RuntimeConfig
from finbot_ingestion.scheduler.market_sessions import ExchangeMarketSessions
from finbot_ingestion.scheduler.window_policy import WindowPolicy
from finbot_ingestion.sec.client import SecClient
from finbot_ingestion.sec.rate_limiter import SECRateLimiter
from finbot_ingestion.config import IngestionConfig


class ReplayClock:
    def __init__(self):
        self.elapsed = 0.0
        self.condition = threading.Condition()
        self.waiters = []
        self.serial = 0

    def now(self):
        return NOW + timedelta(seconds=self.monotonic())

    def monotonic(self):
        with self.condition:
            return self.elapsed

    def advance(self, seconds):
        with self.condition:
            self.elapsed += seconds
            self.condition.notify_all()
        while self.waiters and self.waiters[0][0] <= self.elapsed:
            _, _, waiter = heapq.heappop(self.waiters)
            if not waiter.done():
                waiter.set_result(None)

    def block_sleep(self, seconds):
        with self.condition:
            until = self.elapsed + seconds
            deadline = time.monotonic() + 10
            while self.elapsed < until:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError("replay clock driver stopped")
                self.condition.wait(remaining)

    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        self.serial += 1
        heapq.heappush(self.waiters, (self.monotonic() + seconds, self.serial, future))
        await future


class Response:
    def __init__(self, content, status=200):
        self.content, self.status_code = content, status
        self.headers = {"Content-Type": "application/octet-stream"}

    def close(self):
        pass

    def iter_content(self, **kwargs):
        yield self.content


class Transport:
    def __init__(self, clock, *, heavy=False):
        self.clock, self.headers, self.starts, self.polls = clock, {}, [], {}
        self.throttled = False
        self.failures = 0
        self.heavy = heavy

    def mount(self, *args):
        pass

    def close(self):
        pass

    def get(self, url, **kwargs):
        started = self.clock.monotonic()
        operation = "poll" if "submissions/CIK" in url else "index" if url.endswith("-index.html") else "directory" if url.endswith("index.json") else "download"
        self.starts.append((started, operation))
        if operation == "poll":
            cik = url.split("CIK")[1].split(".")[0]
            self.polls.setdefault(cik, []).append(started)
            if cik == "0000320193" and not self.throttled:
                self.throttled = True
                self.failures += 1
                return Response(b"", 429)
            filing = cik == "0000320193"
            arrays = {"accessionNumber": [ACCESSION] if filing else [], "form": ["8-K"] if filing else [],
                "acceptanceDateTime": [NOW.isoformat()] if filing else [],
                "primaryDocument": ["Primary.htm"] if filing else [], "items": ["9.01"] if filing else []}
            payload = json.dumps({"cik": cik, "filings": {"recent": arrays}}).encode()
        else:
            root = Path(__file__).parents[1] / "fixtures" / "sec_package"
            payload = (root / ("index.html" if operation == "index" else "directory.json")).read_bytes() if operation != "download" else b"original SEC bytes"
            if operation == "directory" and self.heavy:
                directory = json.loads(payload)
                directory["directory"]["item"].extend({"name": f"extra{i:02d}.htm", "type": "file", "size": 18}
                                                      for i in range(32))
                payload = json.dumps(directory).encode()
        self.clock.block_sleep(.4 if operation == "download" else .02)
        return Response(payload)


def percentile(values, q):
    if not values:
        return None
    return sorted(values)[min(len(values) - 1, int((len(values) - 1) * q))]


@pytest.mark.parametrize("active", [5, 10, 25, 50])
@pytest.mark.parametrize("interval", [5, 10])
def test_500_company_capacity_replay(system, tmp_path, active, interval):
    async def scenario():
        clock = ReplayClock()
        companies = [Company("AAPL", "320193", "Apple")] + [Company(f"S{i}", str(i), f"Synthetic {i}") for i in range(1, 500)]
        for company in companies:
            await system.db.companies.upsert(company)
        expectations = [ExpectedEarningsEvent(company_cik=c.cik, ticker=c.ticker, expected_date=NOW.date(),
            time_of_day="after_market", provider="placeholder", synced_at=NOW, provider_event_id="q3") for c in companies[:active]]
        await system.db.calendar.upsert_events(expectations)
        transport = Transport(clock, heavy=active == 50 and interval == 5)
        limiter = SECRateLimiter(clock=clock.monotonic, sleeper=clock.block_sleep)
        sec = SecClient(IngestionConfig("Finbot test test@example.com"), limiter=limiter,
            session=transport, sleeper=clock.block_sleep, random_value=lambda: 0, now=clock.now)
        worker = system.worker()
        worker.discovery.sec = worker.downloader.sec = sec
        latency_samples, queue_samples = [], []
        queue_maxima = {"poll": 0, "filing": 0, "artifact": 0}
        stage_latencies = {name: [] for name in ("DiscoveryLatencyMs", "DownloadLatencyMs", "IngestionLatencyMs")}
        def sink(text):
            payload = json.loads(text)
            latency_samples.extend(payload.get("PollQueueDelayMs", []))
            for name in stage_latencies:
                stage_latencies[name].extend(payload.get(name, []))
        original_s3 = system.s3.api
        def storage(operation, params):
            system.s3.clock = clock.now()
            return original_s3(operation, params)
        # Existing client boundary was bound to old api; replace it on the actual client.
        worker.downloader.store.execution.client._make_api_call = storage
        # Old publication work for a disabled company, hidden until a later GSI pass.
        old_accession = "0000999999-25-000001"
        await system.db.companies.upsert(Company("OFF", "999999", "Disabled", enabled=False))
        old = make_filing(accession_number=old_accession, company_cik="999999", ticker="OFF",
            filed_at=NOW - timedelta(days=360), discovered_at=NOW - timedelta(days=359),
            filing_index_url=filing_index_url("999999", old_accession))
        child = make_artifact("old.htm", accession_number=old_accession, company_cik="999999", ticker="OFF",
            discovered_at=old.discovered_at, sec_url=document_url("999999", old_accession, "old.htm"))
        await system.db.filings.create_if_absent(old)
        await PackageCheckpoint(system.db.filings, system.db.artifacts).persist(old, [child],
            primary_document_name="old.htm", completed_at=NOW - timedelta(days=359))
        stored = await worker.downloader.store.put_if_absent(child, DownloadedDocument(b"old bytes", "text/html", child.sec_url))
        await system.db.artifacts.mark_stored(child.artifact_id, stored.s3_uri, stored.stored_at, stored.content_type, stored.size_bytes)
        system.db.memory.index_snapshots["PendingArtifactWork"] = []
        config = RuntimeConfig(active_poll_seconds=interval, tick_seconds=.1, reload_seconds=10000,
            recovery_seconds=30, shutdown_grace_seconds=.03, heartbeat_seconds=10,
            health_path=str(tmp_path / "health.json"))
        app = RuntimeApplication(config=config, clock=clock, worker=worker, recovery=RecoveryService(worker),
            satisfaction=DynamoDBSatisfactionRepository(system.db.execution, system.db.filings),
            calendar_service=CalendarSyncService.with_placeholder(system.db.companies, system.db.calendar,
                CalendarConfig(provider="placeholder"), now=clock.now), metrics=Metrics(sink=sink, now_ms=lambda: int(clock.now().timestamp() * 1000)),
            windows=WindowPolicy(ExchangeMarketSessions(start=date(2026, 1, 1), end=date(2026, 12, 31)), config))
        task = asyncio.create_task(app.run())
        async with asyncio.timeout(10):
            while not app.tasks:
                if task.done():
                    task.result()
                await asyncio.sleep(.001)
        while clock.monotonic() < 120:
            clock.advance(.1)
            if clock.monotonic() >= 60:
                system.db.memory.index_snapshots.pop("PendingArtifactWork", None)
            queue_samples.append(app.polls.queue.qsize())
            for name, queue in (("poll", app.polls), ("filing", app.filings), ("artifact", app.artifacts)):
                queue_maxima[name] = max(queue_maxima[name], queue.queue.qsize())
            await asyncio.sleep(.001)
            if task.done():
                task.result()
        intervals = [b - a for c in companies[:active] for a, b in zip(transport.polls.get(c.cik, []), transport.polls.get(c.cik, [])[1:])]
        all_starts = [value for value, _ in transport.starts]
        assert all(sum(t <= other <= t + 1 for other in all_starts) <= 5 for t in all_starts)
        assert all(c.cik in transport.polls for c in companies[:active])
        assert max(queue_samples) <= config.company_queue_size
        assert queue_maxima["filing"] <= config.filing_queue_size
        assert queue_maxima["artifact"] <= config.artifact_queue_size
        assert len(app.tasks) == 11
        assert transport.failures == 1 and system.s3.objects
        assert app.last_recovery is not None
        assert (await system.db.artifacts.get(child.artifact_id)).published_at is not None
        report = {"companies": 500, "active": active, "interval_seconds": interval, "duration_seconds": 120,
            "http_attempts": len(all_starts), "http_by_class": {k: sum(op == k for _, op in transport.starts) for k in ("poll", "index", "directory", "download")},
            "queue_delay_p50_seconds": percentile(latency_samples, .5) / 1000 if latency_samples else None,
            "queue_delay_p99_seconds": percentile(latency_samples, .99) / 1000 if latency_samples else None,
            "effective_interval_p50_seconds": percentile(intervals, .5), "effective_interval_p99_seconds": percentile(intervals, .99),
            "max_poll_queue": max(queue_samples), "dynamodb_calls": len(system.db.memory.calls),
            "stored_objects": len(system.s3.objects), "recovery": app.recovery_summary}
        report["delayed_old_disabled_work_published"] = True
        report["extra_exhibits"] = 32 if transport.heavy else 0
        report["task_count"] = len(app.tasks)
        report["max_queues"] = queue_maxima
        report["http_statuses"] = {"429": transport.failures, "200": len(all_starts) - transport.failures}
        report["per_active_company"] = {}
        for company in companies[:active]:
            starts = transport.polls[company.cik]
            gaps = [b - a for a, b in zip(starts, starts[1:])]
            report["per_active_company"][company.cik] = {"attempts": len(starts),
                "interval_p50_seconds": percentile(gaps, .5), "interval_p99_seconds": percentile(gaps, .99)}
        report["stage_latencies_ms"] = {name: {"p50": percentile(values, .5), "p99": percentile(values, .99),
            "samples": len(values)} for name, values in stage_latencies.items()}
        app.request_stop()
        while not task.done():
            clock.advance(1)
            await asyncio.sleep(.001)
        await task
        sec.close()
        print("PHASE6_REPLAY " + json.dumps(report, sort_keys=True))
    asyncio.run(scenario())
