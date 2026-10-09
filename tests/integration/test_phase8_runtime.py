"""Startup cancellation and shutdown recovery over existing stateful boundaries."""
import asyncio
from dataclasses import replace
import time

from test_phase4 import system
from test_dynamodb_repositories import db
from test_phase6 import setup, until


def test_quiet_startup_can_stop_without_sec_calls(system, tmp_path):
    async def scenario():
        app, _ = await setup(system, tmp_path)
        app.config = replace(app.config, sec_startup_quiet_seconds=150)
        task = asyncio.create_task(app.run())
        await asyncio.sleep(.02)
        assert not system.sec.calls and not app.tasks
        app.request_stop()
        async with asyncio.timeout(1):
            await task
        assert not system.sec.calls
    asyncio.run(scenario())


def test_quiet_period_uses_monotonic_clock_before_recovery_and_polling(system, tmp_path):
    async def scenario():
        app, _ = await setup(system, tmp_path)
        class Clock:
            def __init__(self):
                self.elapsed = 0
                self.release = asyncio.Event()
            def monotonic(self):
                return self.elapsed
            def now(self):
                return app.worker.control.now()
            async def sleep(self, seconds):
                if seconds > 100:
                    await self.release.wait()
                else:
                    await asyncio.sleep(seconds)
        clock = Clock()
        app.clock = clock
        app.config = replace(app.config, sec_startup_quiet_seconds=150)
        task = asyncio.create_task(app.run())
        await asyncio.sleep(.02)
        assert not system.sec.calls
        clock.elapsed = 149
        clock.release.set()
        # A spurious/early wakeup must not bypass the deadline.
        clock.release.clear()
        await asyncio.sleep(.02)
        assert not system.sec.calls
        clock.elapsed = 150
        clock.release.set()
        await until(lambda: bool(system.sec.calls))
        app.request_stop()
        await task
    asyncio.run(scenario())
