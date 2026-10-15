"""6-K routing, durable acquisition and calendar linking across restart."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from finbot_ingestion.domain import Company, FilingCheckpoint
from finbot_ingestion.domain.sec_items import FilingObservation, SECItemMetadata
from finbot_ingestion.domain.satisfaction import EventIdentity, EventSatisfaction
from finbot_ingestion.ingestion.recovery_service import RecoveryService
from finbot_ingestion.repositories.errors import RepositoryConflict
from finbot_ingestion.repositories.dynamodb.satisfaction import read_satisfaction, satisfaction_record
from finbot_ingestion.scheduler.earnings_satisfaction_policy import EarningsSatisfactionPolicy, SIX_K_POLICY_VERSION
from finbot_ingestion.sec.client import DownloadedDocument
from finbot_ingestion.sec.errors import SECDocumentTooLarge
from finbot_ingestion.sec.filing_index import parse_filing_index
from finbot_ingestion.selection.policy import AcquisitionPolicy, POLICY_VERSION
from tests.support.dynamodb import make_filing, NOW, ACCESSION
from tests.support.ingestion import run
from tests.support.runtime import setup

COMPANY = Company('AAPL', '320193', 'Synthetic foreign issuer')
FIXTURES = Path(__file__).parents[1] / 'fixtures/sec_6k'


def wire(worker, system, monkeypatch, *, primary=None, rows=None, form='6-K', oversized=False):
    worker.discovery.selection = AcquisitionPolicy(NOW - timedelta(days=1))
    filing = make_filing(form_type=form, primary_document_name='Primary.htm')
    primary = (FIXTURES / 'asml_primary.html').read_bytes() if primary is None else primary
    rows = rows or [('Primary.htm', form, '6-K'), ('release.htm', 'EX-99.1', 'EX-99.1'),
        ('deck.htm', 'EX-99.2', 'EX-99.2'), ('statements.htm', 'EX-99.3', 'EX-99.3'),
        ('interim.pdf', 'EX-99.4', 'EX-99.4'), ('logo.jpg', 'GRAPHIC', 'Logo'),
        (ACCESSION + '.txt', None, 'Complete submission text')]
    html = '<table><tr><th>Description</th><th>Document</th><th>Type</th></tr>'
    html += ''.join(f'<tr><td>{label}</td><td><a href="{name}">{name}</a></td><td>{kind or ""}</td></tr>'
                    for name, kind, label in rows) + '</table>'
    package = parse_filing_index(filing, html.encode(), {'directory': {'item': [{'name': name} for name, _, _ in rows]}})
    downloads = []
    def observations(company):
        return (FilingObservation(filing, SECItemMetadata(ACCESSION, COMPANY.cik)),)
    def download(url, *, max_bytes, context=None):
        name = url.rsplit('/', 1)[-1]
        assert name not in {'deck.htm', 'logo.jpg', ACCESSION + '.txt'}
        downloads.append((name, max_bytes))
        if name == 'Primary.htm' and oversized and max_bytes <= 4 * 1024 * 1024:
            raise SECDocumentTooLarge('primary exceeds routing bound')
        return DownloadedDocument(primary if name == 'Primary.htm' else b'original bytes', 'text/html', url)
    monkeypatch.setattr(system.sec, 'get_company_submissions_with_evidence', observations, raising=False)
    monkeypatch.setattr(system.sec, 'get_filing_index', lambda filing: package)
    monkeypatch.setattr(system.sec, 'download_document', download)
    return filing, observations(COMPANY), downloads


@pytest.mark.parametrize('form', ['6-K', '6-K/A'])
def test_selected_original_bytes_events_and_restart_do_not_reacquire(system, monkeypatch, form):
    worker = system.worker()
    filing, observations, downloads = wire(worker, system, monkeypatch, form=form)
    async def scenario():
        assert await worker.discover_company(COMPANY) == (ACCESSION,)
        assert {name for name, _ in downloads} == {'Primary.htm', 'release.htm', 'statements.htm', 'interim.pdf'}
        assert sum(name == 'Primary.htm' for name, _ in downloads) == 1
        assert downloads[0][1] == 4 * 1024 * 1024
        parent = await system.db.filings.get_checkpoint(ACCESSION)
        assert (parent.selection_policy, parent.selection_reason, parent.enumerated_artifact_count) == (POLICY_VERSION, 'earnings_6_k', 4)
        assert len(system.s3.objects) == 4 and len(system.messages.events) == 1
        assert any('/2026-10-08_' + form.replace('/', '-') + '_' in key for _, key in system.s3.objects)
        assert (FIXTURES / 'asml_primary.html').read_bytes() in [body for body, _ in system.s3.objects.values()]
        restarted = system.worker()
        restarted.discovery.selection = worker.discovery.selection
        await RecoveryService(restarted).run_pass()
        assert len(downloads) == 4 and len(system.messages.events) == 1
    run(scenario())


@pytest.mark.parametrize('primary,outcome', [
    (b'<h1>AGM results 2026</h1><p>99.1 AGM press release</p>', 'EXCLUDE'),
    (b'<h1>Outcome of board meeting</h1><p>The Board approved financial results for Q2 2026.</p>', 'EXCLUDE'),
    (b'<h1>Will announce financial results for Q2 2026</h1>', 'EXCLUDE'),
    (b'<p>Form 6-K</p><p>99.1 Press release</p>', 'UNRESOLVED'),
])
def test_rejected_packages_create_no_children_objects_or_publications(system, monkeypatch, primary, outcome):
    worker = system.worker()
    _, _, downloads = wire(worker, system, monkeypatch, primary=primary,
        rows=[('Primary.htm', '6-K', '6-K'), ('release.htm', 'EX-99.1', 'EX-99.1')])
    async def scenario():
        await worker.discover_company(COMPANY)
        parent = await system.db.filings.get_checkpoint(ACCESSION)
        assert parent.selection_decision == outcome and parent.enumeration_completed_at is None
        assert not (await system.db.filings.list_pending()).items
        assert not system.db.memory.tables['artifacts'] and not system.s3.objects and not system.messages.events
        await RecoveryService(worker).run_pass()
        assert [name for name, _ in downloads] == ['Primary.htm']
    run(scenario())


@pytest.mark.parametrize('label,selected', [('6-K', False), ('First quarter report 2026', True)])
def test_oversized_routing_is_not_terminal_and_index_can_still_qualify(system, monkeypatch, label, selected):
    worker = system.worker()
    _, _, downloads = wire(worker, system, monkeypatch, oversized=True,
        rows=[('Primary.htm', '6-K', label)])
    async def scenario():
        await worker.discover_company(COMPANY)
        parent = await system.db.filings.get_checkpoint(ACCESSION)
        assert parent.terminal_at is None and parent.enumeration_failures == 0
        assert (parent.selection_decision == 'SELECT') is selected
        assert len(downloads) == (2 if selected else 1)
        assert bool(system.messages.events) is selected
    run(scenario())


def test_runtime_links_only_after_classification_and_restores_match_without_io(system, monkeypatch, tmp_path):
    async def scenario():
        app, expected = await setup(system, tmp_path, form='6-K', status='absent', items=())
        filing, _, downloads = wire(app.worker, system, monkeypatch)
        observations = await app.worker.discovery.discover_with_evidence(COMPANY)
        await app.reload()
        await app.satisfy(observations)
        identity = EventIdentity.from_event(expected)
        assert await app.satisfaction.get(identity) is None
        await app.worker.process_filing(ACCESSION)
        await app.satisfy(observations)
        match = await app.satisfaction.get(identity)
        assert match.match_policy_version == SIX_K_POLICY_VERSION and match.match_reason == 'original_6_k_earnings'
        assert not match.matched_sec_items and match.sec_item_evidence_source is None
        assert read_satisfaction(satisfaction_record(match)) == match
        assert app.scheduler.active(COMPANY.cik)
        before = downloads.copy(), system.messages.events.copy()
        await app.reload()
        observations = await app.worker.discovery.discover_with_evidence(COMPANY)
        await app.satisfy(observations)
        assert (downloads, system.messages.events) == before
        assert await app.satisfaction.get(identity) == match
    run(scenario())


def test_conflicting_only_results_document_creates_no_artifacts_or_calendar_match(system, monkeypatch, tmp_path):
    async def scenario():
        app, expected = await setup(system, tmp_path, form='6-K', status='absent', items=())
        _, observations, downloads = wire(app.worker, system, monkeypatch,
            primary=b'<p>99.1 Earnings presentation</p>',
            rows=[('Primary.htm', '6-K', '6-K'),
                  ('release.htm', 'EX-99.1', 'Financial results release for Q2 2026')])
        await app.worker.discover_company(COMPANY)
        await app.reload()
        await app.satisfy(observations)
        parent = await system.db.filings.get_checkpoint(ACCESSION)
        assert (parent.selection_decision, parent.selection_reason) == ('UNRESOLVED', 'unresolved_6_k')
        assert parent.enumeration_completed_at is None
        assert not system.db.memory.tables['artifacts']
        assert not system.s3.objects and not system.messages.events
        assert await app.satisfaction.get(EventIdentity.from_event(expected)) is None
        await RecoveryService(app.worker).run_pass()
        assert [name for name, _ in downloads] == ['Primary.htm']
    run(scenario())


def test_repository_refuses_forged_6k_match_before_earnings_selection(system, tmp_path):
    async def scenario():
        app, expected = await setup(system, tmp_path, form='6-K', status='absent', items=())
        observations = await app.worker.discovery.discover_with_evidence(COMPANY)
        supplied = observations[0]
        progress = FilingCheckpoint(accession_number=ACCESSION, selection_policy=POLICY_VERSION,
            selection_decision='SELECT', selection_reason='earnings_6_k', selection_checked_at=NOW,
            enumeration_completed_at=NOW, resolved_primary_document_name='Primary.htm', enumerated_artifact_count=1)
        policy = EarningsSatisfactionPolicy()
        decision = policy.evaluate(expected, supplied.filing, supplied.sec_items, selection=progress)
        value = EventSatisfaction.from_match(expected, policy.window(expected), supplied, decision, at=NOW)
        with pytest.raises(RepositoryConflict, match='completed earnings selection'):
            await app.satisfaction.create_if_absent(value)
        for version in ['earnings-satisfaction-v1', 'earnings-satisfaction-v2']:
            with pytest.raises(ValueError, match='6-K satisfaction'):
                replace(value, match_policy_version=version)
    run(scenario())


def test_fixed_cutoff_excludes_6k_without_enumeration_or_routing(system, monkeypatch):
    worker = system.worker()
    _, _, downloads = wire(worker, system, monkeypatch)
    worker.discovery.selection = AcquisitionPolicy(NOW + timedelta(seconds=1))
    async def scenario():
        assert await worker.discover_company(COMPANY) == ()
        assert not downloads and not system.db.memory.tables['filings']
    run(scenario())


def test_conflicting_attachment_labels_omit_child_while_clear_documents_proceed(system, monkeypatch):
    worker = system.worker()
    _, _, downloads = wire(worker, system, monkeypatch,
        rows=[('Primary.htm', '6-K', '6-K'), ('release.htm', 'EX-99.1', 'Earnings presentation'),
              ('statements.htm', 'EX-99.3', 'Consolidated financial statements for Q2 2026')])
    async def scenario():
        await worker.discover_company(COMPANY)
        assert {name for name, _ in downloads} == {'Primary.htm', 'statements.htm'}
        assert await system.db.artifacts.get(ACCESSION + '/release.htm') is None
        parent = await system.db.filings.get_checkpoint(ACCESSION)
        assert parent.selection_decision == 'SELECT' and parent.enumerated_artifact_count == 2
    run(scenario())
