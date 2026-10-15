"""Filters replayed through real repositories/S3/SNS adapters over offline fakes."""

from dataclasses import replace
from datetime import timedelta
import asyncio

import pytest

from finbot_ingestion.domain import Company
from finbot_ingestion.domain.sec_items import FilingObservation, SECItemMetadata
from finbot_ingestion.ingestion.recovery_service import RecoveryService
from finbot_ingestion.ingestion.work_control import Outcome
from finbot_ingestion.repositories.errors import RepositoryConflict
from finbot_ingestion.repositories.package_checkpoint import PackageCheckpoint
from finbot_ingestion.sec.client import DownloadedDocument
from finbot_ingestion.sec.errors import SECDocumentTooLarge
from finbot_ingestion.sec.filing_index import parse_filing_index
from finbot_ingestion.selection.policy import AcquisitionPolicy, Decision, POLICY_VERSION

from tests.support.dynamodb import make_filing, make_artifact, NOW, ACCESSION
from tests.support.ingestion import seed, run


COMPANY = Company(cik='320193', ticker='AAPL', name='Apple')
PRIMARY = b'''<p><b>Item 2.02 Results of Operations and Financial Condition</b></p>
<p>Financial results were announced in a press release attached as Exhibit 99.1.</p>
<table><tr><td>99.1</td><td><a href="release.htm">Press Release dated October 8</a></td></tr></table>'''


def selected_worker(system, monkeypatch, *, items=('2.02', '9.01'), index_items='2.02', generic=False,
                    rows=None, primary=PRIMARY):
    worker = system.worker()
    worker.discovery.selection = AcquisitionPolicy(NOW - timedelta(seconds=10))
    filing = make_filing(primary_document_name='Primary.htm')
    if rows is None:
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
    def download(url, *, max_bytes, context=None):
        assert context == {"ticker": filing.ticker, "cik": filing.company_cik,
            "accession_number": filing.accession_number,
            "artifact_id": filing.accession_number + "/" + url.rsplit('/', 1)[-1]}
        name = url.rsplit('/', 1)[-1]
        assert name in {'Primary.htm', 'release.htm', 'supplement.pdf'}, 'excluded URL was requested'
        downloads.append(name)
        content = primary if name == 'Primary.htm' else b'original release or supplement'
        assert len(content) <= max_bytes
        return DownloadedDocument(content, 'text/html' if name.endswith('.htm') else 'application/pdf', url)
    monkeypatch.setattr(system.sec, 'get_company_submissions_with_evidence', observations, raising=False)
    monkeypatch.setattr(system.sec, 'get_filing_index', index)
    monkeypatch.setattr(system.sec, 'download_document', download)
    return worker, filing, parsed, downloads


async def persist_legacy(system, filing, children, *, primary_document_name, completed_at):
    children = tuple(children)
    for child in children:
        await system.db.artifacts.create_if_absent(child)
    await system.db.filings.mark_enumerated(filing.accession_number,
        primary_document_name=primary_document_name, artifact_count=len(children), completed_at=completed_at)


def test_full_filtered_pipeline_never_requests_excluded_documents(system, monkeypatch):
    worker, filing, _, downloads = selected_worker(system, monkeypatch)
    async def scenario():
        assert await worker.discover_company(COMPANY) == (ACCESSION,)
        assert set(downloads) == {'Primary.htm', 'release.htm', 'supplement.pdf'}
        assert len(system.s3.objects) == 3 and len(system.messages.events) == 1
        parent = await system.db.filings.get_checkpoint(ACCESSION)
        assert parent.enumerated_artifact_count == 3 and parent.selection_decision == 'SELECT'
        children = (await system.db.artifacts.list_for_filing(ACCESSION)).items
        assert {child.filename for child in children} == set(downloads)
        assert all(child.published_at is not None for child in children)
        await RecoveryService(worker).run_pass()
        assert len(downloads) == 3 and len(system.messages.events) == 1
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


@pytest.mark.parametrize('rows,primary,expected', [
    ([('Primary.htm', '8-K', 'Primary'), ('release.htm', 'EX-99.1', 'EX-99.1')],
     b'<div>Item 2.02 Results</div><p>Exhibit 99.1.</p><div>Item 9.01 Exhibits</div>'
     b'<a href="release.htm">Press release announcing quarterly financial results</a>',
     {'Primary.htm', 'release.htm'}),
    ([('Primary.htm', '8-K', 'Primary'), ('release.htm', 'EX-99.1', 'News Release dated October 13'),
      ('supplement.pdf', 'EX-99.2', '3Q26 Quarterly Supplement'), ('Slides.PDF', 'EX-99.3', 'Presentation'),
      ('other.htm', 'EX-99.4', 'News release'), ('hyperlink.htm', 'EX-99.5', 'Hyperlinked PDF')],
     b'<div>Item 2.02 Results</div><p>Exhibits 99.1 and 99.2 and 99.5.</p>'
     b'<div>Item 7.01 Other</div><p>Exhibit 99.4.</p>',
     {'Primary.htm', 'release.htm', 'supplement.pdf'}),
    ([('Primary.htm', '8-K', 'Primary'), ('release.htm', 'EX-99.1', 'Earnings release'),
      ('supplement.pdf', 'EX-99.2', 'EX-99.2')],
     b'<div>Item 2.02 Results</div><p>Exhibit 99.1.</p><div>Item 9.01 Exhibits</div>'
     b'<table><tr><td>99.2</td><td><a href="supplement.pdf">Unaudited Comparative Supplementary '
     b'Sales Data and Condensed Consolidated Statement of Earnings</a></td></tr></table>',
     {'Primary.htm', 'release.htm', 'supplement.pdf'}),
], ids=['dal-link', 'wfc-item-context', 'jnj-supplement'])
def test_v2_evidence_selects_only_results_documents_and_survives_restart(system, monkeypatch, rows, primary, expected):
    worker, filing, _, downloads = selected_worker(system, monkeypatch, rows=rows, primary=primary)
    async def scenario():
        await worker.discover_company(COMPANY)
        assert set(downloads) == expected
        assert downloads.count('Primary.htm') == 1
        assert len(downloads) == len(system.s3.objects) == len(expected)
        assert len(system.messages.events) == 1
        parent = await system.db.filings.get_checkpoint(ACCESSION)
        assert parent.selection_policy == POLICY_VERSION and parent.enumerated_artifact_count == len(expected)
        children = (await system.db.artifacts.list_for_filing(ACCESSION)).items
        assert {child.filename for child in children} == expected
        assert all(child.stored_at is not None and child.published_at is not None for child in children)
        # A fresh worker repeats pending recovery without re-downloading or publishing.
        restarted = system.worker()
        restarted.discovery.selection = worker.discovery.selection
        await RecoveryService(restarted).run_pass()
        assert len(downloads) == len(expected) and len(system.messages.events) == 1
    run(scenario())


def test_v1_completed_package_is_not_automatically_reenumerated(system, monkeypatch):
    worker, filing, parsed, downloads = selected_worker(system, monkeypatch, generic=True)
    async def scenario():
        await system.db.filings.create_if_absent(filing)
        await system.db.filings.record_selection(ACCESSION, Decision('SELECT', 'earnings_item_2_02'), NOW,
            policy_version='earnings-acquisition-v1')
        primary = next(a for a in parsed.artifacts(filing, discovered_at=NOW) if a.filename == 'Primary.htm')
        await persist_legacy(system, filing, (primary,),
            primary_document_name='Primary.htm', completed_at=NOW)
        assert (await worker.enumerate_only(ACCESSION)).outcome == Outcome.COMPLETE
        assert await system.db.artifacts.get(ACCESSION + '/release.htm') is None
        parent = await system.db.filings.get_checkpoint(ACCESSION)
        assert parent.enumerated_artifact_count == 1 and parent.selection_policy == 'earnings-acquisition-v1'
        assert not system.sec.calls and not downloads and not system.messages.events
    run(scenario())


def test_v1_pending_package_upgrades_and_preserves_admission_evidence(system, monkeypatch):
    worker, filing, _, downloads = selected_worker(system, monkeypatch, generic=True, index_items=None)
    # The original affirmative submissions evidence remains valid when the index
    # and primary don't repeat the heading. V2 changes attachment roles only.
    monkeypatch.setattr(system.sec, 'download_document', lambda url, **kwargs:
        DownloadedDocument(b'<table><tr><td>99.1</td><td><a href="release.htm">Earnings release</a>'
                           b'</td></tr></table>', 'text/html', url))
    async def scenario():
        await system.db.filings.create_if_absent(filing)
        await system.db.filings.record_selection(ACCESSION, Decision('SELECT', 'earnings_item_2_02'), NOW,
            policy_version='earnings-acquisition-v1')
        assert await worker.process_filing(ACCESSION) == Outcome.COMPLETE
        parent = await system.db.filings.get_checkpoint(ACCESSION)
        assert parent.selection_policy == POLICY_VERSION and parent.selection_reason == 'earnings_item_2_02'
        assert parent.enumerated_artifact_count == 3
        assert len(system.s3.objects) == 3 and len(system.messages.events) == 1
        for name in ('Primary.htm', 'release.htm', 'supplement.pdf'):
            progress = await system.db.artifacts.get_checkpoint(ACCESSION + '/' + name)
            assert progress.selection_policy == POLICY_VERSION
        with pytest.raises(RepositoryConflict):
            await system.db.filings.record_selection(ACCESSION, Decision('SELECT', 'earnings_item_2_02'), NOW,
                policy_version='earnings-acquisition-v1')
    run(scenario())


@pytest.mark.parametrize('decision', [Decision('EXCLUDE', 'non_earnings_8_k'),
                                     Decision('UNRESOLVED', 'unresolved_items')])
def test_v2_does_not_readmit_v1_blocked_work(system, monkeypatch, decision):
    worker, filing, _, downloads = selected_worker(system, monkeypatch)
    async def scenario():
        await system.db.filings.create_if_absent(filing)
        await system.db.filings.record_selection(ACCESSION, decision, NOW, policy_version='earnings-acquisition-v1')
        before = await system.db.filings.get_checkpoint(ACCESSION)
        assert await worker.process_filing(ACCESSION) == Outcome.SKIPPED
        with pytest.raises(RepositoryConflict):
            await system.db.filings.record_selection(ACCESSION, Decision('SELECT', 'earnings_item_2_02'), NOW,
                policy_version=POLICY_VERSION)
        assert await system.db.filings.get_checkpoint(ACCESSION) == before
        assert not system.sec.calls and not downloads and not system.messages.events
    run(scenario())


@pytest.mark.parametrize('stored', [False, True])
def test_v1_pending_child_upgrades_without_replacing_stored_bytes(system, monkeypatch, stored):
    worker, filing, parsed, downloads = selected_worker(system, monkeypatch, generic=True)
    async def scenario():
        await system.db.filings.create_if_absent(filing)
        await system.db.filings.record_selection(ACCESSION, Decision('SELECT', 'earnings_item_2_02'), NOW,
            policy_version='earnings-acquisition-v1')
        children = tuple(a for a in parsed.artifacts(filing, discovered_at=NOW)
                         if a.filename in {'Primary.htm', 'release.htm', 'supplement.pdf'})
        await persist_legacy(system, filing, children,
            primary_document_name='Primary.htm', completed_at=NOW)
        child = next(a for a in children if a.filename == 'release.htm')
        await system.db.artifacts.record_selection(child.artifact_id, Decision('SELECT', 'earnings_release'), NOW,
            policy_version='earnings-acquisition-v1')
        if stored:
            saved = await system.store.put_if_absent(child, filing,
                DownloadedDocument(b'original stored release', 'text/html', child.sec_url))
            await system.db.artifacts.mark_stored(child.artifact_id, saved.s3_uri, saved.stored_at,
                saved.content_type, saved.size_bytes)
        before = await system.db.artifacts.get(child.artifact_id)
        assert await worker.process_artifact(child.artifact_id) == Outcome.COMPLETE
        after = await system.db.artifacts.get(child.artifact_id)
        progress = await system.db.artifacts.get_checkpoint(child.artifact_id)
        assert progress.selection_policy == POLICY_VERSION and progress.selection_reason == 'earnings_release'
        assert after.stored_at is not None and after.published_at is None
        assert not system.messages.events
        if stored:
            assert (after.s3_uri, after.stored_at, after.size_bytes) == (before.s3_uri, before.stored_at, before.size_bytes)
            assert 'release.htm' not in downloads
        else:
            assert downloads.count('release.htm') == 1
        assert (await system.db.filings.get_checkpoint(ACCESSION)).enumerated_artifact_count == 3
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
        assert len(system.messages.events) == 1
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
        assert len(system.messages.events) == 1
    run(scenario())


@pytest.mark.parametrize('stored', [False, True])
def test_legacy_pending_attachment_is_removed_from_indexes_without_erasing_facts(system, monkeypatch, stored):
    worker, filing, parsed, downloads = selected_worker(system, monkeypatch)
    async def scenario():
        await system.db.filings.create_if_absent(filing)
        children = parsed.artifacts(filing, discovered_at=NOW)
        await persist_legacy(system, filing, children,
            primary_document_name='Primary.htm', completed_at=NOW)
        identity = ACCESSION + '/graphic.jpg'
        if stored:
            child = await system.db.artifacts.get(identity)
            saved = await system.store.put_if_absent(child, filing, DownloadedDocument(b'old graphic', 'image/jpeg', child.sec_url))
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
    def oversized(url, *, max_bytes, context=None):
        if url.endswith('/release.htm'):
            raise SECDocumentTooLarge('SEC document exceeds MAX_ARTIFACT_BYTES')
        return original(url, max_bytes=max_bytes, context=context)
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
        await persist_legacy(system, filing,
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
        await persist_legacy(system, filing,
            parsed.artifacts(filing, discovered_at=NOW), primary_document_name='Primary.htm', completed_at=NOW)
        summary = await RecoveryService(worker, page_size=1).run_pass()
        assert summary.skipped == 3 and summary.complete == 3 and summary.errors == 0
        assert set(downloads) == {'Primary.htm', 'release.htm', 'supplement.pdf'}
        assert (await system.db.filings.get_checkpoint(ACCESSION)).enumerated_artifact_count == 6
        assert not (await system.db.artifacts.list_pending('ACQUIRE')).items
        assert not (await system.db.artifacts.list_pending('PUBLISH')).items
        assert (await RecoveryService(worker, page_size=1).run_pass()).candidates == 0
    run(scenario())


def test_publication_waits_for_all_selected_documents(system, monkeypatch):
    worker, filing, _, downloads = selected_worker(system, monkeypatch, generic=True)
    async def scenario():
        await worker.discovery.discover_with_evidence(COMPANY)
        await worker.enumerate_only(ACCESSION)
        await worker.process_artifact(ACCESSION + '/Primary.htm')
        await worker.process_artifact(ACCESSION + '/release.htm')
        assert not system.messages.events
        await worker.process_artifact(ACCESSION + '/supplement.pdf')
        assert len(system.messages.events) == 1
        assert len(downloads) == 3
    run(scenario())
