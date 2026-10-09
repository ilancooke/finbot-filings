"""Supervised runtime -> real adapters over offline stateful SDK boundaries."""

import asyncio
from dataclasses import replace
from datetime import date, timedelta
import json
import time

import boto3
from botocore.exceptions import ReadTimeoutError
from botocore.stub import Stubber
import pytest

from test_dynamodb_repositories import db, make_filing, NOW, ACCESSION, CONFIG
from test_phase4 import system
from test_phase5 import FakeProvider, event, APPLE
from finbot_ingestion.calendar import CalendarConfig
from finbot_ingestion.calendar.service import CalendarSyncService
from finbot_ingestion.domain.sec_items import SECItemMetadata, FilingObservation
from finbot_ingestion.domain.satisfaction import EventIdentity, EventSatisfaction
from finbot_ingestion.ingestion.recovery_service import RecoveryService
from finbot_ingestion.repositories.dynamodb import DynamoDBExecution, DynamoDBFilingRepository
from finbot_ingestion.repositories.dynamodb.satisfaction import (
    DynamoDBSatisfactionRepository, satisfaction_record, read_satisfaction, SATISFACTION_PARTITION,
)
from finbot_ingestion.repositories.dynamodb.serialization import encode, record
from finbot_ingestion.repositories.errors import RepositoryConflict, RepositoryDataError
from finbot_ingestion.observability.metrics import Metrics
from finbot_ingestion.runtime.application import RuntimeApplication
from finbot_ingestion.runtime.config import RuntimeConfig
from finbot_ingestion.scheduler.market_sessions import ExchangeMarketSessions
from finbot_ingestion.scheduler.window_policy import WindowPolicy
from finbot_ingestion.scheduler.earnings_satisfaction_policy import EarningsSatisfactionPolicy


class TestClock:
    __test__ = False

    def now(self):
        return NOW

    def monotonic(self):
        return time.monotonic()

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


async def until(predicate, *, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.005)


async def setup(system, tmp_path, *, status="known", items=("2.02",), form="8-K", provider="fixture"):
    system.sec.filings = [make_filing(form_type=form)]
    def evidence(company):
        system.sec._call("submissions")
        return tuple(FilingObservation(f, SECItemMetadata(f.accession_number, f.company_cik, status, items))
                     for f in system.sec.filings)
    system.sec.get_company_submissions_with_evidence = evidence
    await system.db.companies.upsert(APPLE)
    expected = event(time_of_day="after_market", provider=provider)
    await system.db.calendar.upsert_events([expected])
    config = RuntimeConfig(tick_seconds=.005, active_poll_seconds=.01, safety_poll_seconds=1,
        metrics_flush_seconds=.005, heartbeat_seconds=.005, recovery_seconds=100, reload_seconds=100,
        refresh_retry_seconds=100, shutdown_grace_seconds=.1, health_path=str(tmp_path / "health.json"))
    service = (CalendarSyncService(FakeProvider([expected]), system.db.companies, system.db.calendar,
               CalendarConfig(provider="fixture"), now=lambda: NOW) if provider == "fixture" else
               CalendarSyncService.with_placeholder(system.db.companies, system.db.calendar, now=lambda: NOW))
    worker = system.worker()
    app = RuntimeApplication(config=config, calendar_service=service,
        satisfaction=DynamoDBSatisfactionRepository(system.db.execution, system.db.filings), worker=worker,
        recovery=RecoveryService(worker), clock=TestClock(), metrics=Metrics(sink=lambda _: None),
        windows=WindowPolicy(ExchangeMarketSessions(start=date(2026, 1, 1), end=date(2026, 12, 31)), config))
    return app, expected


@pytest.mark.parametrize("form,status,items,eligible", [("8-K", "known", ("2.02",), True),
    ("8-K", "known", ("9.01",), False), ("8-K", "absent", (), False),
    ("8-K", "ambiguous", (), False), ("8-K/A", "known", ("2.02",), False),
    ("10-Q", "absent", (), True), ("10-K", "absent", (), True),
    ("10-Q/A", "absent", (), False), ("10-K/A", "absent", (), False)])
def test_runtime_satisfaction_and_independent_ingestion(system, tmp_path, form, status, items, eligible):
    async def scenario():
        app, expected = await setup(system, tmp_path, form=form, status=status, items=items)
        task = asyncio.create_task(app.run())
        await until(lambda: "submissions" in system.sec.calls)
        # Await evidence-aware discovery and matching, not merely HTTP invocation.
        await until(lambda: bool(app.scheduler.states) and app.scheduler.states[APPLE.cik].due > app.clock.monotonic())
        identity = EventIdentity.from_event(expected)
        assert (await app.satisfaction.get(identity) is not None) is eligible
        assert app.scheduler.active(APPLE.cik) is (not eligible)
        assert await system.db.filings.get(ACCESSION) is not None
        if form in ("8-K", "8-K/A"):
            await until(lambda: bool(system.messages.events))
            assert system.s3.objects  # non-matches still acquire and publish
        app.request_stop()
        await task
        assert not (tmp_path / "health.json").exists()
        assert all(t.done() for t in app.tasks.values())
    asyncio.run(scenario())


def test_satisfaction_restart_refresh_date_move_and_lost_ack(system, tmp_path):
    async def scenario():
        app, expected = await setup(system, tmp_path)
        observations = await app.worker.discovery.discover_with_evidence(APPLE)
        await app.reload()
        system.db.memory.lost.append("PutItem")
        await app.satisfy(observations)
        original = await app.satisfaction.get(EventIdentity.from_event(expected))
        assert original.match_reason == "original_8_k_item_2_02" and original.matched_sec_items == ("2.02",)
        moved = replace(expected, expected_date=expected.expected_date + timedelta(days=1), synced_at=NOW + timedelta(seconds=1))
        await system.db.calendar.upsert_events([moved])
        await system.db.calendar.cancel_event(expected, at=NOW + timedelta(seconds=1))
        restarted, _ = await setup(system, tmp_path)
        # setup refresh writes older observations, which cannot replace the tombstone.
        await restarted.reload()
        assert EventIdentity.from_event(moved) in restarted.scheduler.satisfied
        assert await restarted.satisfaction.get(EventIdentity.from_event(moved)) == original
        malformed = satisfaction_record(original)
        malformed.pop("match_policy_version")
        with pytest.raises(RepositoryDataError):
            read_satisfaction(malformed)
        malformed = satisfaction_record(original)
        malformed["match_reason"] = "any_filing"
        with pytest.raises(RepositoryDataError):
            read_satisfaction(malformed)
    asyncio.run(scenario())


def test_satisfaction_sdk_wire_condition_strong_read_and_missing_parent():
    async def scenario():
        filing = make_filing()
        expected = event(time_of_day="after_market")
        config = RuntimeConfig()
        window = WindowPolicy(ExchangeMarketSessions(start=date(2026, 1, 1), end=date(2026, 12, 31)), config).window(expected)
        observation = FilingObservation(filing, SECItemMetadata(ACCESSION, APPLE.cik, "known", ("2.02",)))
        decision = EarningsSatisfactionPolicy().evaluate(expected, window, filing, observation.sec_items)
        value = EventSatisfaction.from_match(expected, window, observation, decision, at=NOW)
        assert len(json.dumps(encode(satisfaction_record(value))).encode()) < 2048
        client = boto3.client("dynamodb", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
        with DynamoDBExecution(client, CONFIG) as execution, Stubber(client) as stub:
            filings = DynamoDBFilingRepository(execution)
            repo = DynamoDBSatisfactionRepository(execution, filings)
            parent = {**record(filing), "revision": 0, "retry_count": 0, "pending_work_kind": "ENUMERATE",
                "pending_work_sort": filing.discovered_at.isoformat(timespec="microseconds").replace("+00:00", "Z") + "/" + ACCESSION}
            get = {"TableName": "filings", "Key": encode({"accession_number": ACCESSION}), "ConsistentRead": True}
            stub.add_response("get_item", {"Item": encode(parent)}, get)
            stub.add_response("put_item", {}, {"TableName": "calendar", "Item": encode(satisfaction_record(value)),
                "ConditionExpression": "attribute_not_exists(#key)", "ExpressionAttributeNames": {"#key": "expected_date"}})
            assert await repo.create_if_absent(value)
            stub.add_response("get_item", {"Item": encode(satisfaction_record(value))}, {"TableName": "calendar",
                "Key": encode({"expected_date": SATISFACTION_PARTITION, "cik": value.identity.key}), "ConsistentRead": True})
            assert await repo.get(value.identity) == value
            stub.add_response("get_item", {}, get)
            with pytest.raises(RepositoryConflict):
                await repo.create_if_absent(value)
            stub.assert_no_pending_responses()
    asyncio.run(scenario())


@pytest.mark.parametrize("loop", ["scheduler_loop", "refresh_loop", "reload_loop", "recovery_loop",
    "poll_loop", "enumeration_loop", "artifact_loop", "heartbeat_loop", "metrics_loop"])
@pytest.mark.parametrize("fails", [True, False])
def test_required_task_failure_or_return_cannot_leave_idle_process(system, tmp_path, loop, fails):
    async def scenario():
        app, _ = await setup(system, tmp_path)
        async def broken(*args):
            if fails:
                raise RuntimeError("injected failure")
        setattr(app, loop, broken)
        with pytest.raises(RuntimeError):
            await app.run()
        assert all(t.done() for t in app.tasks.values())
        assert not app.health_snapshot()["live"]
    asyncio.run(scenario())


def test_placeholder_degraded_scope_health_and_no_tick_database_reads(system, tmp_path):
    async def scenario():
        app, _ = await setup(system, tmp_path, provider="placeholder")
        task = asyncio.create_task(app.run())
        await until(lambda: bool(app.tasks))
        await until(lambda: app.startup_recovery_complete)
        snapshot = app.health_snapshot()
        assert snapshot["live"] and snapshot["calendar_stale"] and not snapshot["provider_configured"]
        before = len(system.db.memory.calls)
        for _ in range(10):
            app.scheduler.tick()
        assert len(system.db.memory.calls) == before
        app.request_stop()
        await task
    asyncio.run(scenario())


def test_overlapping_expectations_and_unknown_policy_do_not_suppress(system, tmp_path):
    async def scenario():
        app, expected = await setup(system, tmp_path)
        observed = await app.worker.discovery.discover_with_evidence(APPLE)
        await app.reload()
        app.scheduler.events += (replace(expected, provider_event_id="other"),)
        await app.satisfy(observed)
        assert not app.scheduler.satisfied
        app.scheduler.events = (expected,)
        await app.satisfy(observed)
        key = (SATISFACTION_PARTITION, EventIdentity.from_event(expected).key)
        system.db.memory.tables["calendar"][key]["match_policy_version"] = "future"
        with pytest.raises(RepositoryDataError):
            await app.reload()
    asyncio.run(scenario())


def test_shutdown_waits_for_blocking_call_and_repeated_cancellation(system, tmp_path):
    import threading
    async def scenario():
        app, _ = await setup(system, tmp_path)
        started, release = threading.Event(), threading.Event()
        original = system.sec.get_company_submissions_with_evidence
        def blocked(company):
            started.set()
            release.wait(2)
            return original(company)
        system.sec.get_company_submissions_with_evidence = blocked
        task = asyncio.create_task(app.run())
        await until(started.is_set)
        task.cancel()
        await asyncio.sleep(.02)
        task.cancel()
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert app.worker.discovery.execution._active == 0
        assert all(t.done() for t in app.tasks.values())
    asyncio.run(scenario())


def test_main_entrypoint_and_health_check_without_aws(monkeypatch, tmp_path):
    from finbot_ingestion.main import main
    from finbot_ingestion.observability.health import HealthFile
    monkeypatch.setenv("RUNTIME_HEALTH_PATH", str(tmp_path / "health.json"))
    assert main(["--health-check"]) == 1
    health = HealthFile(tmp_path / "health.json")
    from datetime import datetime, timezone
    health.write({"live": True, "heartbeat_at": datetime.now(timezone.utc).isoformat()})
    assert main(["--health-check"]) == 0
    class FakeApp:
        def request_stop(self):
            pass
        async def run(self):
            return
    assert main([], builder=lambda stack: FakeApp()) == 0
    def broken(stack):
        raise ValueError("credential-bearing URL must not be printed")
    assert main([], builder=broken) == 1


def test_signal_handler_stops_admissions_and_health_stall(system, tmp_path, monkeypatch):
    import signal
    from finbot_ingestion.main import serve
    async def scenario():
        app, _ = await setup(system, tmp_path)
        handlers = {}
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback: handlers.update({sig: callback}))
        monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: handlers.pop(sig))
        task = asyncio.create_task(serve(app))
        await until(lambda: bool(app.tasks))
        app.activity["poll-0"] = (app.clock.monotonic() - app.config.stall_seconds - 1, True)
        assert not app.health_snapshot()["live"]
        app.activity["poll-0"] = (app.clock.monotonic(), False)
        handlers[signal.SIGTERM]()
        assert not app.polls.accepting
        await task
        assert not handlers
    asyncio.run(scenario())


def test_calendar_health_checks_scope_and_full_freshness(system, tmp_path):
    async def scenario():
        app, _ = await setup(system, tmp_path)
        await app.calendar_service.sync_full()
        await app.reload()
        assert app.scope_matches and not app.health_snapshot()["calendar_stale"]
        # New company invalidates full-success coverage even after a near-term refresh.
        from finbot_ingestion.domain import Company
        await system.db.companies.upsert(Company("NEW", "9", "New"))
        await app.calendar_service.sync_near_term()
        await app.reload()
        assert not app.scope_matches and app.health_snapshot()["calendar_stale"]
    asyncio.run(scenario())
