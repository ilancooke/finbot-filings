"""Durable stage failure semantics over real repository CAS and SDK paging."""

import asyncio
from copy import deepcopy
from datetime import timedelta

import pytest
from botocore.exceptions import ReadTimeoutError

from test_dynamodb_repositories import db, make_filing, make_artifact, NOW, ACCESSION
from finbot_ingestion.repositories.errors import RepositoryConflict, RepositoryDataError


def test_stage_failure_idempotency_terminal_and_dead_letter_checkpoint(db):
    async def scenario():
        child = make_artifact()
        await db.artifacts.create_if_absent(child)
        async def failure(at=NOW, error="failure"):
            await db.artifacts.record_stage_failure(child.artifact_id, "ACQUIRE", error, at,
                max_failures=2, error_type="SECNetworkError")
        db.memory.lost.append("UpdateItem")
        with pytest.raises(ReadTimeoutError):
            await failure()
        before = deepcopy(db.memory.tables)
        await failure()
        assert db.memory.tables == before
        with pytest.raises(RepositoryConflict):
            await failure(error="different")
        await failure(NOW - timedelta(days=1), "older")
        assert (await db.artifacts.get_checkpoint(child.artifact_id)).acquisition_failures == 1
        await failure(NOW + timedelta(seconds=1))
        assert not (await db.artifacts.list_pending("ACQUIRE")).items
        assert (await db.artifacts.list_pending("DEAD_LETTER")).items
        db.memory.lost.append("UpdateItem")
        at = NOW + timedelta(seconds=2)
        with pytest.raises(ReadTimeoutError):
            await db.artifacts.mark_dead_lettered(child.artifact_id, at)
        await db.artifacts.mark_dead_lettered(child.artifact_id, NOW + timedelta(days=1))
        assert (await db.artifacts.get_checkpoint(child.artifact_id)).dead_lettered_at == at
        assert not (await db.artifacts.list_pending("DEAD_LETTER")).items
        with pytest.raises(RepositoryConflict):
            await db.artifacts.mark_stored(child.artifact_id, "s3://bucket/key", NOW, None, 1)
        await db.artifacts.record_failure(child.artifact_id, "late", NOW + timedelta(days=1))
        assert (await db.artifacts.get(child.artifact_id)).retry_count == 2
    asyncio.run(scenario())


def test_late_acquisition_failure_cannot_terminalize_publication_or_complete_work(db):
    async def scenario():
        child = make_artifact()
        await db.artifacts.create_if_absent(child)
        await db.artifacts.mark_stored(child.artifact_id, "s3://bucket/key", NOW, None, 1)
        await db.artifacts.record_stage_failure(child.artifact_id, "ACQUIRE", "late", NOW,
            max_failures=1, error_type="SECNetworkError")
        assert (await db.artifacts.get_checkpoint(child.artifact_id)).terminal_at is None
        await db.artifacts.mark_published(child.artifact_id, NOW)
        await db.artifacts.record_stage_failure(child.artifact_id, "PUBLISH", "late", NOW,
            max_failures=1, error_type="ReadTimeoutError")
        assert (await db.artifacts.get_checkpoint(child.artifact_id)).publication_failures == 0
    asyncio.run(scenario())


def test_filing_terminal_blocks_completion_and_wrong_stage(db):
    async def scenario():
        await db.filings.create_if_absent(make_filing())
        with pytest.raises(ValueError):
            await db.filings.record_stage_failure(ACCESSION, "ACQUIRE", "wrong", NOW,
                max_failures=1, error_type="error")
        await db.filings.record_stage_failure(ACCESSION, "ENUMERATE", "unsafe", NOW,
            max_failures=3, error_type="SECDataError", terminal=True)
        assert (await db.filings.get_checkpoint(ACCESSION)).enumeration_failures == 1
        with pytest.raises(RepositoryConflict):
            await db.filings.mark_enumerated(ACCESSION, primary_document_name="Primary.htm", artifact_count=1, completed_at=NOW)
        from finbot_ingestion.repositories.package_checkpoint import PackageCheckpoint
        with pytest.raises(RepositoryConflict):
            await PackageCheckpoint(db.filings, db.artifacts).persist(make_filing(), [make_artifact()],
                primary_document_name="Primary.htm", completed_at=NOW)
        assert not db.memory.tables["artifacts"]
        with pytest.raises(ValueError):
            await db.filings.list_pending(kind="ACQUIRE")
    asyncio.run(scenario())


def test_dead_letter_checkpoint_requires_terminal_work(db):
    async def scenario():
        await db.filings.create_if_absent(make_filing())
        await db.artifacts.create_if_absent(make_artifact())
        with pytest.raises(RepositoryConflict):
            await db.filings.mark_dead_lettered(ACCESSION, NOW)
        with pytest.raises(RepositoryConflict):
            await db.artifacts.mark_dead_lettered(make_artifact().artifact_id, NOW)
    asyncio.run(scenario())


def test_failure_cas_race_rechecks_stage_and_cannot_undo_progress(db):
    async def scenario():
        child = make_artifact()
        await db.artifacts.create_if_absent(child)
        def advance(item):
            item.update(revision=item["revision"] + 1, s3_uri="s3://bucket/key",
                stored_at="2026-10-08T20:00:00.000000Z", pending_work_kind="PUBLISH")
        db.memory.before_update = advance
        await db.artifacts.record_stage_failure(child.artifact_id, "ACQUIRE", "late", NOW,
            max_failures=1, error_type="SECNetworkError")
        assert (await db.artifacts.get_checkpoint(child.artifact_id)).terminal_at is None
        assert len((await db.artifacts.list_pending("PUBLISH")).items) == 1
    asyncio.run(scenario())


def test_stage_budget_does_not_rewind_newer_legacy_failure_diagnostics(db):
    async def scenario():
        child = make_artifact()
        await db.artifacts.create_if_absent(child)
        later = NOW + timedelta(days=1)
        await db.artifacts.record_failure(child.artifact_id, "newer diagnostic", later)
        await db.artifacts.record_stage_failure(child.artifact_id, "ACQUIRE", "stage failure", NOW,
            max_failures=3, error_type="SECNetworkError")
        source = await db.artifacts.get(child.artifact_id)
        assert source.retry_count == 1 and source.last_error_at == later
        assert (await db.artifacts.get_checkpoint(child.artifact_id)).acquisition_failures == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [{"acquisition_failures": "1"}, {"failure_at": None},
    {"terminal_at": "2026-10-08T20:00:00.000000Z"}, {"publication_failures": 1}])
def test_malformed_phase4_checkpoint_fields_are_rejected(db, changes):
    async def scenario():
        await db.artifacts.create_if_absent(make_artifact())
        next(iter(db.memory.tables["artifacts"].values())).update(changes)
        with pytest.raises((RepositoryDataError, RepositoryConflict)):
            await db.artifacts.get(make_artifact().artifact_id)
    asyncio.run(scenario())
