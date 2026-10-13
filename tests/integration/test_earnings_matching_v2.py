"""Calendar-day matching over existing downloads and mocked durable repositories."""

import asyncio
from dataclasses import replace
from datetime import timedelta

from test_dynamodb_repositories import db, NOW, ACCESSION, make_filing
from test_phase4 import system
from test_phase6 import setup, until
from test_phase5 import APPLE
from finbot_ingestion.domain.satisfaction import EventIdentity, EventSatisfaction
from finbot_ingestion.repositories.dynamodb.satisfaction import satisfaction_record, read_satisfaction


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
        before = len(system.db.memory.calls)
        await app.satisfy(observations)
        assert len(system.db.memory.calls) == before
        assert await app.satisfaction.get(legacy.identity) == legacy
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
