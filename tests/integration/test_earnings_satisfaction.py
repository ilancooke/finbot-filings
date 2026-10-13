"""Durable earnings matching, compatibility and post-match polling."""

from dataclasses import replace
from datetime import timedelta
import asyncio

import pytest

from finbot_ingestion.domain.satisfaction import EventIdentity, EventSatisfaction
from finbot_ingestion.repositories.dynamodb.satisfaction import (
    satisfaction_record,
    read_satisfaction,
    SATISFACTION_PARTITION,
)
from finbot_ingestion.repositories.errors import RepositoryDataError
from finbot_ingestion.sec.urls import filing_index_url

from tests.support.calendar import APPLE
from tests.support.dynamodb import make_filing, NOW, ACCESSION
from tests.support.runtime import until, setup


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
        assert app.scheduler.active(APPLE.cik)  # Recent matches retain frequent follow-up polling.
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




def test_restart_matches_already_downloaded_early_filing_without_redownload(system, tmp_path):
    async def scenario():
        app, expected = await setup(system, tmp_path)
        early = make_filing(filed_at=NOW.replace(hour=10, minute=30), discovered_at=NOW.replace(hour=10, minute=40))
        system.sec.filings = [early]
        observed = await app.worker.discovery.discover_with_evidence(APPLE)
        await app.worker.process_filing(ACCESSION)
        assert system.s3.objects and system.messages.events
        calls = system.sec.calls.copy()
        objects = system.s3.objects.copy()
        publications = system.messages.events.copy()
        assert not app.windows.window(expected).contains(early.filed_at)
        assert await app.satisfaction.get(EventIdentity.from_event(expected)) is None

        # The normal poll loop returns canonical observations even for completed
        # parents. It must create a match without dispatching the package again.
        task = asyncio.create_task(app.run())
        await until(lambda: EventIdentity.from_event(expected) in app.scheduler.satisfied)
        app.request_stop()
        await task
        checkpoint = await app.satisfaction.get(EventIdentity.from_event(expected))
        assert checkpoint.match_policy_version == "earnings-satisfaction-v2"
        assert checkpoint.matched_filed_at == early.filed_at
        assert system.sec.calls.count("submissions") > calls.count("submissions")
        assert system.sec.calls.count("index") == calls.count("index")
        assert system.sec.calls.count("download") == calls.count("download")
        assert system.s3.objects == objects and system.messages.events == publications
        assert read_satisfaction(satisfaction_record(checkpoint)) == checkpoint
    asyncio.run(scenario())


def test_v1_checkpoint_survives_reload_and_is_not_rewritten(system, tmp_path):
    async def scenario():
        app, expected = await setup(system, tmp_path)
        app.clock = app.scheduler.clock = FollowUpClock()
        observations = await app.worker.discovery.discover_with_evidence(APPLE)
        window = app.windows.window(expected)
        legacy = EventSatisfaction(identity=EventIdentity.from_event(expected),
            matched_accession_number=ACCESSION, matched_filed_at=observations[0].filing.filed_at,
            satisfied_at=NOW, observed_expected_date=expected.expected_date,
            window_start=window.start, window_end=window.end, grace_end=window.grace_end,
            matched_form_type="8-K", match_reason="original_8_k_item_2_02",
            match_policy_version="earnings-satisfaction-v1", matched_sec_items=("2.02",),
            sec_item_evidence_source="submissions.recent.items")
        await app.satisfaction.create_if_absent(legacy)
        assert read_satisfaction(satisfaction_record(legacy)) == legacy
        await app.reload()
        assert EventIdentity.from_event(expected) in app.scheduler.satisfied
        assert app.scheduler.active(APPLE.cik)
        assert app.scheduler.satisfied_at[legacy.identity] == legacy.satisfied_at
        before = len(system.db.memory.calls)
        await app.satisfy(observations)
        assert len(system.db.memory.calls) == before
        assert await app.satisfaction.get(legacy.identity) == legacy
        app.clock.elapsed = 600
        await app.reload()
        assert not app.scheduler.active(APPLE.cik)
    asyncio.run(scenario())


def test_one_accession_cannot_satisfy_another_event_after_reload(system, tmp_path):
    async def scenario():
        app, expected = await setup(system, tmp_path)
        observations = await app.worker.discovery.discover_with_evidence(APPLE)
        await app.reload()
        await app.satisfy(observations)
        original = await app.satisfaction.get(EventIdentity.from_event(expected))
        # Move the already-satisfied provider ID to another date. Its checkpoint
        # still owns the accession, even though its current date no longer overlaps.
        moved = replace(expected, expected_date=expected.expected_date + timedelta(days=1), synced_at=NOW + timedelta(seconds=1))
        other = replace(expected, provider_event_id="different-event", synced_at=NOW + timedelta(seconds=1))
        await system.db.calendar.cancel_event(expected, at=NOW)
        await system.db.calendar.upsert_events([moved, other])
        await app.reload()
        assert other in app.scheduler.events
        await app.satisfy(observations)
        assert await app.satisfaction.get(EventIdentity.from_event(other)) is None
        assert await app.satisfaction.get(original.identity) == original
    asyncio.run(scenario())


class FollowUpClock:
    def __init__(self):
        self.elapsed = 0

    def now(self):
        return NOW + timedelta(seconds=self.elapsed)

    def monotonic(self):
        return self.elapsed


def test_10q_match_continues_polling_and_acquires_companion_8k(system, tmp_path):
    async def scenario():
        app, expected = await setup(system, tmp_path, form="10-Q", status="absent", items=())
        app.clock = app.scheduler.clock = FollowUpClock()
        app.worker.control.now = app.clock.now
        observations = await app.worker.discovery.discover_with_evidence(APPLE)
        await app.reload()
        await app.satisfy(observations)
        identity = EventIdentity.from_event(expected)
        first = await app.satisfaction.get(identity)
        app.scheduler.completed(APPLE.cik)
        assert app.scheduler.active(APPLE.cik)

        # Companion report appears 33 seconds after the first match, as in AZZ.
        app.clock.elapsed = 33
        accession = "0000320193-26-000002"
        companion = make_filing(accession_number=accession, filed_at=app.clock.now(),
                                filing_index_url=filing_index_url(APPLE.cik, accession))
        system.sec.filings.append(companion)
        app.scheduler.tick()
        assert app.polls.contains(APPLE.cik)
        task = asyncio.create_task(app.poll_loop("poll-0"))
        try:
            await until(lambda: not app.polls.contains(APPLE.cik))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert app.filings.contains(companion.accession_number)
        await app.worker.process_filing(companion.accession_number)
        assert system.s3.objects and system.messages.events
        assert await app.satisfaction.get(identity) == first
        assert app.scheduler.satisfied_at[identity] == first.satisfied_at

        app.clock.elapsed = 600
        assert not app.scheduler.active(APPLE.cik)
        app.scheduler.completed(APPLE.cik)
        assert app.scheduler.states[APPLE.cik].due == 600 + app.config.safety_poll_seconds
    asyncio.run(scenario())


def test_reload_and_compatible_replay_use_original_durable_match_time(system, tmp_path):
    async def scenario():
        app, expected = await setup(system, tmp_path)
        app.clock = app.scheduler.clock = FollowUpClock()
        observations = await app.worker.discovery.discover_with_evidence(APPLE)
        await app.reload()
        await app.satisfy(observations)
        identity = EventIdentity.from_event(expected)
        first = await app.satisfaction.get(identity)
        app.clock.elapsed = 300
        await app.reload()
        assert app.scheduler.active(APPLE.cik)
        assert app.scheduler.satisfied_at[identity] == first.satisfied_at
        app.clock.elapsed = 600
        await app.reload()
        assert not app.scheduler.active(APPLE.cik)

        # Simulate a compatible checkpoint already stored before this process
        # learned of the acknowledgment. Creating it again must not restart polling.
        app.scheduler.satisfied.clear()
        app.satisfied_accessions.clear()
        await app.satisfy(observations)
        assert app.scheduler.satisfied_at[identity] == first.satisfied_at
        assert not app.scheduler.active(APPLE.cik)
        assert await app.satisfaction.get(identity) == first
    asyncio.run(scenario())
