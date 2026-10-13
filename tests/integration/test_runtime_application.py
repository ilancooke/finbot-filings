"""Supervised runtime, health and cooperative shutdown."""

import asyncio

import pytest

from tests.support.runtime import until, setup


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
