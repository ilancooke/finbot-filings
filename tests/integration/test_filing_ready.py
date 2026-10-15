"""Whole-filing readiness, immutable retries and explicit later snapshots."""

import asyncio
from dataclasses import replace
from datetime import timedelta
import json

import pytest
from botocore.exceptions import ReadTimeoutError

from finbot_ingestion.domain import Company
from finbot_ingestion.domain.filing_ready import FilingReady
from finbot_ingestion.ingestion.recovery_service import RecoveryService
from finbot_ingestion.ingestion.work_control import Outcome
from finbot_ingestion.repositories.package_checkpoint import PackageCheckpoint
from finbot_ingestion.sec.errors import SECDataError
from tests.support.dynamodb import ACCESSION, NOW, make_artifact, make_filing
from tests.support.ingestion import run


async def package(system):
    filing = make_filing(primary_document_name="Primary.htm")
    children = tuple(make_artifact(name) for name in ("Primary.htm", "release.htm", "supplement.pdf"))
    await system.db.filings.create_if_absent(filing)
    await PackageCheckpoint(system.db.filings, system.db.artifacts).persist(filing, children,
        primary_document_name="Primary.htm", completed_at=NOW)
    return filing, children


def events(system):
    return [json.loads(params["Message"]) for operation, params in system.messages.events if operation == "Publish"]


def test_three_files_emit_one_event_only_after_every_file_is_stored(system):
    async def scenario():
        _, children = await package(system)
        worker = system.worker()
        await worker.process_artifact(children[1].artifact_id)
        await worker.process_artifact(children[0].artifact_id)
        assert not events(system)
        # Hide all index entries: readiness uses the exact saved membership and
        # strong reads, not index counts or an S3 listing.
        system.db.memory.index_snapshots["ArtifactsByAccession"] = []
        await worker.process_artifact(children[2].artifact_id)
        notification, = events(system)
        assert notification["event_type"] == "filing.ready"
        assert notification["primary_artifact_id"] == children[0].artifact_id
        assert [child["artifact_id"] for child in notification["artifacts"]] == sorted(child.artifact_id for child in children)
        for entry in notification["artifacts"]:
            durable = await system.db.artifacts.get(entry["artifact_id"])
            assert (entry["s3_uri"], entry["size_bytes"]) == (durable.s3_uri, durable.size_bytes)
        assert FilingReady.from_json((await system.db.filings.get_ready_event(ACCESSION)).to_json()).to_dict() == notification
        await RecoveryService(system.worker()).run_pass()
        assert len(events(system)) == 1 and system.sec.calls.count("download") == 3
    run(scenario())


def test_failed_selected_file_prevents_partial_filing_event(system):
    async def scenario():
        _, children = await package(system)
        worker = system.worker()
        await worker.process_artifact(children[0].artifact_id)
        system.sec.failures.append(("download", SECDataError("invalid selected file")))
        assert await worker.process_artifact(children[1].artifact_id) == Outcome.TERMINAL
        await worker.process_artifact(children[2].artifact_id)
        await RecoveryService(system.worker()).run_pass()
        assert not events(system)
        assert (await system.db.filings.get_checkpoint(ACCESSION)).ready_snapshot_id is None
    run(scenario())


@pytest.mark.parametrize("boundary", ["saved_payload", "ready_pointer", "sns", "published_pointer", "child_pointer"])
def test_restart_reuses_saved_notification_and_never_downloads_again(system, monkeypatch, boundary):
    async def scenario():
        _, children = await package(system)
        worker = system.worker()
        for child in children:
            await worker._store_artifact(child.artifact_id)
        original = system.db.memory.api
        if boundary == "sns":
            system.messages.lost.append("Publish")
        else:
            def lose(operation, params):
                result = original(operation, params)
                names = params.get("ExpressionAttributeNames", {}).values()
                matched = (operation == "PutItem" and boundary == "saved_payload") or (
                    operation == "UpdateItem" and {
                        "ready_pointer": "ready_snapshot_id", "published_pointer": "ready_published_at",
                        "child_pointer": "published_at"}.get(boundary) in names)
                if matched:
                    monkeypatch.setattr(system.db.client, "_make_api_call", original)
                    raise ReadTimeoutError(endpoint_url="https://mock.invalid")
                return result
            monkeypatch.setattr(system.db.client, "_make_api_call", lose)
        async def stop(_):
            raise asyncio.CancelledError
        worker.control.backoff = stop
        try:
            await worker.publish_filing(ACCESSION)
        except asyncio.CancelledError:
            pass
        # Change wall time and mutable company input before recovery.
        await system.db.companies.upsert(Company("NEW", "320193", "Renamed company"))
        restarted = system.worker()
        restarted.control.now = lambda: NOW + timedelta(days=1)
        await RecoveryService(restarted, page_size=1).run_pass()
        notifications = events(system)
        assert notifications and all(event == notifications[0] for event in notifications)
        assert notifications[0]["ready_at"] == "2026-10-08T20:00:02.000000Z"
        assert notifications[0]["ticker"] == "AAPL" and notifications[0]["company_name"] == "Apple"
        assert system.sec.calls.count("download") == 3
        assert (await system.db.filings.get_checkpoint(ACCESSION)).ready_published_at is not None
    run(scenario())


def test_later_attachment_emits_a_new_complete_snapshot_and_keeps_the_first(system):
    async def scenario():
        filing, children = await package(system)
        worker = system.worker()
        await asyncio.gather(*(worker.process_artifact(child.artifact_id) for child in children))
        first, = events(system)
        addition = make_artifact("later.htm")
        await PackageCheckpoint(system.db.filings, system.db.artifacts).persist(filing, (*children, addition),
            primary_document_name="Primary.htm", completed_at=NOW + timedelta(days=1))
        await worker.publish_filing(ACCESSION)
        assert len(events(system)) == 1
        await worker.process_artifact(addition.artifact_id)
        second = events(system)[1]
        assert second["snapshot_id"] != first["snapshot_id"]
        assert len(second["artifacts"]) == 4 and len(first["artifacts"]) == 3
        original = await system.db.filings._get(system.db.filings._ready_key(ACCESSION, first["snapshot_id"]))
        assert json.loads(original["event_json"]) == first
        assert system.sec.calls.count("download") == 4
        progress = await system.db.filings.get_checkpoint(ACCESSION)
        assert progress.enumeration_completed_at == NOW and progress.enumerated_artifact_count == 3
    run(scenario())


def test_oversized_notification_fails_explicitly_without_a_partial_event(system):
    async def scenario():
        _, children = await package(system)
        row = system.db.memory.tables['filings'][(ACCESSION,)]
        row['company_name'] = 'x' * (256 * 1024)
        worker = system.worker()
        for child in children:
            await worker.process_artifact(child.artifact_id)
        assert not events(system)
        state = await system.db.filings.get_checkpoint(ACCESSION)
        assert state.terminal_stage == 'PUBLISH' and state.dead_lettered_at is not None
        assert system.sec.calls.count('download') == 3
    run(scenario())
