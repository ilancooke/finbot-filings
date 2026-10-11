"""Filters replayed through real repositories/S3/SNS adapters over offline fakes."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

from botocore.exceptions import ReadTimeoutError
import pytest

from test_dynamodb_repositories import db, make_filing, make_artifact, NOW, ACCESSION
from test_phase4 import system, seed, run
from finbot_ingestion.domain import Company
from finbot_ingestion.domain.sec_items import FilingObservation, SECItemMetadata
from finbot_ingestion.ingestion.recovery_service import RecoveryService
from finbot_ingestion.ingestion.work_control import Outcome
from finbot_ingestion.repositories.package_checkpoint import PackageCheckpoint
from finbot_ingestion.repositories.errors import RepositoryConflict
from finbot_ingestion.sec.client import DownloadedDocument
from finbot_ingestion.sec.errors import SECDocumentTooLarge, SECNetworkError
from finbot_ingestion.sec.filing_index import parse_filing_index
from finbot_ingestion.selection.policy import AcquisitionPolicy, Decision, POLICY_VERSION

COMPANY = Company(cik='320193', ticker='AAPL', name='Apple')
PRIMARY = b'''<p><b>Item 2.02 Results of Operations and Financial Condition</b></p>
<p>Financial results were announced in a press release attached as Exhibit 99.1.</p>
<table><tr><td>99.1</td><td><a href="release.htm">Press Release dated October 8</a></td></tr></table>'''


def selected_worker(system, monkeypatch, *, items=('2.02', '9.01'), index_items='2.02', generic=False):
    worker = system.worker()
    worker.discovery.selection = AcquisitionPolicy(NOW - timedelta(seconds=10))
    filing = make_filing(primary_document_name='Primary.htm')
    rows = [('Primary.htm', '8-K', 'Primary'), ('release.htm', 'EX-99.1', 'EX-99.1' if generic else 'Earnings release'),
            ('supplement.pdf', 'EX-99.2', 'Financial supplement'), ('Slides.PDF', 'EX-99.3', 'Earnings presentation'),
            ('graphic.jpg', 'GRAPHIC', 'Logo'), (ACCESSION + '.txt', '', 'Complete submission text')]
    html = '<div class="infoHead">Items</div><div class="info">'
    html += ('Item ' + index_items + ': Results') if index_items else ''
    html += '</div><table><tr><th>Description</th><th>Document</th><th>Type</th></tr>'
    html += ''.join(f'<tr><td>{description}</td><td><a href="{name}">{name}</a></td><td>{kind}</td></tr>' for name, kind, description in rows) + '</table>'
    parsed = parse_filing_index(filing, html.encode(), {'directory': {'item': [{'name': name} for name, _, _ in rows]}})
    if index_items is None:
        parsed = replace(parsed, items=(), item_status='absent')
    evidence = SECItemMetadata(ACCESSION, filing.company_cik,
        status='known' if items else 'absent', items=items)
    def observations(company):
        system.sec.calls.append('submissions')
        return (FilingObservation(filing, evidence),)
    def index(filing):
        system.sec.calls.append('index')
        return parsed
    downloads = []
    def download(url, *, max_bytes):
        name = url.rsplit('/', 1)[-1]
        assert name in {'Primary.htm', 'release.htm', 'supplement.pdf'}, 'excluded URL was requested'
        downloads.append(name)
        content = PRIMARY if name == 'Primary.htm' else b'original release or supplement'
        assert len(content) <= max_bytes
        return DownloadedDocument(content, 'text/html' if name.endswith('.htm') else 'application/pdf', url)
    monkeypatch.setattr(system.sec, 'get_company_submissions_with_evidence', observations, raising=False)
    monkeypatch.setattr(system.sec, 'get_filing_index', index)
    monkeypatch.setattr(system.sec, 'download_document', download)
    return worker, filing, parsed, downloads


def test_full_filtered_pipeline_never_requests_excluded_documents(system, monkeypatch):
    worker, filing, _, downloads = selected_worker(system, monkeypatch)
    async def scenario():
        assert await worker.discover_company(COMPANY) == (ACCESSION,)
        assert set(downloads) == {'Primary.htm', 'release.htm', 'supplement.pdf'}
        assert len(system.s3.objects) == len(system.messages.events) == 3
        parent = await system.db.filings.get_checkpoint(ACCESSION)
        assert parent.enumerated_artifact_count == 3 and parent.selection_decision == 'SELECT'
        children = (await system.db.artifacts.list_for_filing(ACCESSION)).items
        assert {child.filename for child in children} == set(downloads)
        assert all(child.published_at is not None for child in children)
        await RecoveryService(worker).run_pass()
        assert len(downloads) == 3 and len(system.messages.events) == 3
    run(scenario())


def test_generic_exhibit_uses_primary_bytes_once_and_survives_restart(system, monkeypatch):
    worker, filing, _, downloads = selected_worker(system, monkeypatch, generic=True)
    async def scenario():
        await worker.discover_company(COMPANY)
        assert downloads.count('Primary.htm') == 1
        assert downloads.count('release.htm') == 1
        primary = await system.db.artifacts.get(ACCESSION + '/Primary.htm')
        assert primary.stored_at is not None and primary.published_at is not None
        await RecoveryService(worker).run_pass()
        assert len(downloads) == 3
    run(scenario())


def test_known_non_earnings_and_old_filings_create_no_pending_work(system, monkeypatch):
    worker, filing, _, downloads = selected_worker(system, monkeypatch, items=('8.01', '9.01'))
    async def scenario():
        assert not await worker.discovery.discover_with_evidence(COMPANY)
        assert await system.db.filings.get(ACCESSION) is None
        assert system.sec.calls == ['submissions'] and not downloads
        worker.discovery.selection = AcquisitionPolicy(NOW)
        assert not await worker.discovery.discover_with_evidence(COMPANY)
        assert not (await system.db.filings.list_pending()).items
    run(scenario())


def test_missing_items_uses_index_without_changing_satisfaction_evidence(system, monkeypatch):
    worker, _, _, downloads = selected_worker(system, monkeypatch, items=())
    async def scenario():
        observations = await worker.discovery.discover_with_evidence(COMPANY)
        assert observations[0].sec_items.status == 'absent'
        await worker.process_filing(ACCESSION)
        assert len(downloads) == 3
        assert (await system.db.filings.get_checkpoint(ACCESSION)).selection_decision == 'SELECT'
    run(scenario())


def test_missing_index_items_can_use_structural_primary_and_reuse_bytes(system, monkeypatch):
    worker, _, _, downloads = selected_worker(system, monkeypatch, items=(), index_items=None)
    async def scenario():
        await worker.discover_company(COMPANY)
        assert downloads.count('Primary.htm') == 1
        assert len(system.messages.events) == 3
    run(scenario())


def test_conflicting_items_are_blocked_without_failure_or_attachments(system, monkeypatch):
    worker, _, _, downloads = selected_worker(system, monkeypatch, index_items='8.01')
    async def scenario():
        await worker.discover_company(COMPANY)
        parent = await system.db.filings.get_checkpoint(ACCESSION)
        assert parent.selection_decision == 'UNRESOLVED'
        assert parent.selection_reason == 'conflicting_items'
        assert parent.enumeration_completed_at is None and parent.terminal_at is None
        assert not (await system.db.filings.list_pending()).items
        assert not downloads and not system.messages.events
    run(scenario())


def test_ambiguous_exhibit_keeps_primary_but_leaves_no_download_work(system, monkeypatch):
    worker, _, parsed, downloads = selected_worker(system, monkeypatch)
    parsed = replace(parsed, documents=tuple(replace(d, description='EX-99.2') if d.filename == 'supplement.pdf' else d for d in parsed.documents))
    monkeypatch.setattr(system.sec, 'get_filing_index', lambda filing: parsed)
    async def scenario():
        await worker.discover_company(COMPANY)
        assert set(downloads) == {'Primary.htm', 'release.htm'}
        assert await system.db.artifacts.get(ACCESSION + '/supplement.pdf') is None
        assert len(system.messages.events) == 2
    run(scenario())


@pytest.mark.parametrize('stored', [False, True])
def test_legacy_pending_attachment_is_removed_from_indexes_without_erasing_facts(system, monkeypatch, stored):
    worker, filing, parsed, downloads = selected_worker(system, monkeypatch)
    async def scenario():
        await system.db.filings.create_if_absent(filing)
        children = parsed.artifacts(filing, discovered_at=NOW)
        await PackageCheckpoint(system.db.filings, system.db.artifacts).persist(filing, children,
            primary_document_name='Primary.htm', completed_at=NOW)
        identity = ACCESSION + '/graphic.jpg'
        if stored:
            child = await system.db.artifacts.get(identity)
            saved = await system.store.put_if_absent(child, DownloadedDocument(b'old graphic', 'image/jpeg', child.sec_url))
            await system.db.artifacts.mark_stored(identity, saved.s3_uri, saved.stored_at, saved.content_type, saved.size_bytes)
        old = await system.db.artifacts.get(identity)
        assert await worker.process_artifact(identity) == Outcome.SKIPPED
        progress = await system.db.artifacts.get_checkpoint(identity)
        assert progress.selection_reason == 'standalone_graphic' and progress.terminal_at is None
        assert await system.db.artifacts.get(identity) == old
        assert identity not in {a.artifact_id for a in (await system.db.artifacts.list_pending('ACQUIRE')).items}
        assert identity not in {a.artifact_id for a in (await system.db.artifacts.list_pending('PUBLISH')).items}
        assert not downloads and not system.messages.events
        assert (await system.db.filings.get_checkpoint(ACCESSION)).enumerated_artifact_count == len(children)
        # Clear the metadata cache to simulate restart. The skipped checkpoint is enough.
        worker.discovery._decisions.clear()
        system.sec.calls.clear()
        assert await worker.process_artifact(identity) == Outcome.SKIPPED
        assert not system.sec.calls
        with pytest.raises(RepositoryConflict):
            await system.db.artifacts.mark_published(identity, NOW)
    run(scenario())


@pytest.mark.parametrize('complete', [False, True])
def test_old_parent_and_child_are_skipped_even_from_direct_recovery(system, monkeypatch, complete):
    worker, _, _, downloads = selected_worker(system, monkeypatch)
    worker.discovery.selection = AcquisitionPolicy(NOW)
    async def scenario():
        child = await seed(system, complete=complete)
        assert await worker.process_artifact(child.artifact_id) == Outcome.SKIPPED
        assert await worker.process_filing(ACCESSION) == Outcome.SKIPPED
        assert not (await system.db.artifacts.list_pending('ACQUIRE')).items
        assert not (await system.db.filings.list_pending()).items
        assert not system.sec.calls and not downloads and not system.messages.events
    run(scenario())


def test_published_and_terminal_history_are_untouched(system, monkeypatch):
    worker, _, _, downloads = selected_worker(system, monkeypatch)
    async def scenario():
        child = await seed(system)
        await system.db.artifacts.record_stage_failure(child.artifact_id, 'ACQUIRE', 'old oversized bundle', NOW,
            max_failures=1, error_type='SECDocumentTooLarge')
        await system.db.artifacts.mark_dead_lettered(child.artifact_id, NOW)
        old = await system.db.artifacts.get_checkpoint(child.artifact_id)
        assert await worker.process_artifact(child.artifact_id) == Outcome.TERMINAL
        assert await system.db.artifacts.get_checkpoint(child.artifact_id) == old
        other = make_artifact('already-published.htm')
        await system.db.artifacts.create_if_absent(other)
        await system.db.artifacts.mark_stored(other.artifact_id, 's3://artifacts/' + other.artifact_id, NOW, 'text/html', 10)
        await system.db.artifacts.mark_published(other.artifact_id, NOW)
        assert await worker.process_artifact(other.artifact_id) == Outcome.COMPLETE
        assert not system.sec.calls and not downloads and not system.messages.events
    run(scenario())


def test_selected_oversized_release_retains_terminal_failure_path(system, monkeypatch):
    worker, filing, parsed, downloads = selected_worker(system, monkeypatch)
    original = system.sec.download_document
    def oversized(url, *, max_bytes):
        if url.endswith('/release.htm'):
            raise SECDocumentTooLarge('SEC document exceeds MAX_ARTIFACT_BYTES')
        return original(url, max_bytes=max_bytes)
    monkeypatch.setattr(system.sec, 'download_document', oversized)
    async def scenario():
        await worker.discover_company(COMPANY)
        progress = await system.db.artifacts.get_checkpoint(ACCESSION + '/release.htm')
        assert progress.terminal_stage == 'ACQUIRE' and progress.acquisition_failures == 1
        assert progress.dead_lettered_at is not None
        assert sum(op == 'SendMessage' for op, _ in system.messages.events) == 1
    run(scenario())


def test_lost_skip_acknowledgment_replays_and_does_not_reactivate(system, monkeypatch):
    worker, _, _, downloads = selected_worker(system, monkeypatch)
    async def scenario():
        child = await seed(system)
        worker.discovery.selection = AcquisitionPolicy(NOW)
        system.db.memory.lost.append('UpdateItem')
        assert await worker.process_artifact(child.artifact_id) == Outcome.SKIPPED
        assert await worker.process_artifact(child.artifact_id) == Outcome.SKIPPED
        progress = await system.db.artifacts.get_checkpoint(child.artifact_id)
        assert progress.acquisition_failures == 0 and progress.terminal_at is None
        at = progress.selection_checked_at
        await system.db.artifacts.record_selection(child.artifact_id, Decision('EXCLUDE', 'before_live_cutoff'), NOW + timedelta(days=1), policy_version=POLICY_VERSION)
        assert (await system.db.artifacts.get_checkpoint(child.artifact_id)).selection_checked_at == at
        with pytest.raises(RepositoryConflict):
            await system.db.artifacts.record_selection(child.artifact_id, Decision('SELECT', 'primary_report'), NOW, policy_version=POLICY_VERSION)
        assert not downloads
    run(scenario())


def test_concurrent_legacy_children_share_metadata_and_primary_bytes(system, monkeypatch):
    worker, filing, parsed, downloads = selected_worker(system, monkeypatch, generic=True)
    async def scenario():
        await system.db.filings.create_if_absent(filing)
        await PackageCheckpoint(system.db.filings, system.db.artifacts).persist(filing,
            parsed.artifacts(filing, discovered_at=NOW), primary_document_name='Primary.htm', completed_at=NOW)
        outcomes = await asyncio.wait_for(asyncio.gather(
            worker.process_artifact(ACCESSION + '/release.htm'),
            worker.process_artifact(ACCESSION + '/Primary.htm')), timeout=5)
        assert outcomes == [Outcome.COMPLETE, Outcome.COMPLETE]
        assert downloads.count('Primary.htm') == 1 and downloads.count('release.htm') == 1
        assert system.sec.calls.count('index') == 1
    run(scenario())


def test_recovery_pass_drains_legacy_exclusions_and_keeps_original_snapshot(system, monkeypatch):
    worker, filing, parsed, downloads = selected_worker(system, monkeypatch)
    async def scenario():
        await system.db.filings.create_if_absent(filing)
        await PackageCheckpoint(system.db.filings, system.db.artifacts).persist(filing,
            parsed.artifacts(filing, discovered_at=NOW), primary_document_name='Primary.htm', completed_at=NOW)
        summary = await RecoveryService(worker, page_size=1).run_pass()
        assert summary.skipped == 3 and summary.complete == 3 and summary.errors == 0
        assert set(downloads) == {'Primary.htm', 'release.htm', 'supplement.pdf'}
        assert (await system.db.filings.get_checkpoint(ACCESSION)).enumerated_artifact_count == 6
        assert not (await system.db.artifacts.list_pending('ACQUIRE')).items
        assert not (await system.db.artifacts.list_pending('PUBLISH')).items
        assert (await RecoveryService(worker, page_size=1).run_pass()).candidates == 0
    run(scenario())


def test_primary_routing_waits_while_publication_retains_all_download_slots(system, monkeypatch):
    worker, filing, parsed, downloads = selected_worker(system, monkeypatch, generic=True)
    async def scenario():
        await worker.discovery.discover_with_evidence(COMPANY)
        await worker.enumerate_only(ACCESSION)
        active, both_publishing, release = 0, asyncio.Event(), asyncio.Event()
        publish = worker.publisher.publish_artifact_ready
        async def blocked_publish(event):
            nonlocal active
            active += 1
            if active == 2:
                both_publishing.set()
            await release.wait()
            await publish(event)
        monkeypatch.setattr(worker.publisher, 'publish_artifact_ready', blocked_publish)
        jobs = [asyncio.create_task(worker.process_artifact(ACCESSION + '/' + name))
                for name in ('Primary.htm', 'release.htm')]
        await asyncio.wait_for(both_publishing.wait(), timeout=5)
        next_filing = replace(filing, accession_number='0000320193-26-000002',
            filing_index_url=filing.filing_index_url.replace(ACCESSION, '0000320193-26-000002'))
        await system.db.filings.create_if_absent(next_filing)
        requests_before = system.sec.calls.count('index')
        enumeration = asyncio.create_task(worker.enumerate_only(next_filing.accession_number))
        try:
            await asyncio.sleep(.02)
            assert not enumeration.done()
            assert system.sec.calls.count('index') == requests_before
        finally:
            release.set()
            enumeration.cancel()
            await asyncio.gather(enumeration, return_exceptions=True)
            await asyncio.wait_for(asyncio.gather(*jobs), timeout=5)
    run(scenario())
