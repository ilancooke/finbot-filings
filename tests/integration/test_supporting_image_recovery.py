"""Stopped-service image recovery previews, refusals and interrupted resumes."""

from copy import deepcopy
from types import SimpleNamespace

from botocore.exceptions import ReadTimeoutError
import pytest

from scripts import recover_supporting_images as recovery
from finbot_ingestion.domain import Company
from finbot_ingestion.domain.filing_ready import FilingReady
from finbot_ingestion.ingestion.recovery_service import RecoveryService
from finbot_ingestion.repositories.package_checkpoint import PackageCheckpoint
from finbot_ingestion.sec.client import DownloadedDocument
from finbot_ingestion.sec.filing_index import FilingIndex, PackageDocument
from finbot_ingestion.sec.urls import document_url
from finbot_ingestion.selection.policy import AcquisitionPolicy, Decision
from tests.support.dynamodb import make_filing, ACCESSION, NOW
from tests.support.ingestion import run

TARGET = {'task_definition': 'reviewed:19', 'policy': recovery.POLICY_VERSION, 'image': 'reviewed-image'}


@pytest.fixture
def seeded(system, monkeypatch):
    filing = make_filing(primary_document_name='Primary.htm')
    monkeypatch.setattr(recovery, 'TARGETS', {ACCESSION: (filing.ticker, filing.company_cik, filing.form_type)})
    package = FilingIndex(tuple(PackageDocument(name, document_url(filing.company_cik, ACCESSION, name), kind)
        for name, kind in [('Primary.htm', '8-K'), ('page.jpg', 'GRAPHIC'), ('logo.jpg', 'GRAPHIC')]),
        'Primary.htm', ('2.02',), 'known')
    source = b'<img src="logo.jpg" alt="logo"><img src="page.jpg" width="1365" height="769">'
    monkeypatch.setattr(system.sec, 'get_filing_index', lambda filing: package)
    worker = system.worker()
    worker.discovery.selection = AcquisitionPolicy(filing.filed_at)
    async def seed():
        await system.db.filings.create_if_absent(filing)
        await system.db.filings.record_selection(ACCESSION, Decision('SELECT', 'earnings_item_2_02'), NOW,
            policy_version='earnings-acquisition-v2')
        primary = package.artifacts(filing, discovered_at=NOW)[0]
        await PackageCheckpoint(system.db.filings, system.db.artifacts).persist(filing, [primary],
            primary_document_name=primary.filename, completed_at=NOW)
        await system.db.artifacts.record_selection(primary.artifact_id, Decision('SELECT', 'primary_report'), NOW,
            policy_version='earnings-acquisition-v2')
        await system.db.artifacts.mark_stored(primary.artifact_id, 's3://existing/Primary.htm', NOW, 'text/html', len(source))
        primary = await system.db.artifacts.get(primary.artifact_id)
        event = FilingReady.from_artifacts(filing, [primary], primary_document_name='Primary.htm', company_name='Apple', ready_at=NOW)
        await system.db.filings.save_ready_event(event)
        await system.db.filings.mark_ready_published(ACCESSION, event.snapshot_id, NOW)
        await system.db.artifacts.mark_published(primary.artifact_id, NOW)
    run(seed())
    sources = {'body': source}
    async def read_html(artifact):
        return sources['body']
    downloads = []
    def download(url, **kwargs):
        assert url.endswith('/page.jpg')
        downloads.append(url)
        return DownloadedDocument(b'\xff\xd8\xffSynthetic page', 'image/jpeg', url)
    monkeypatch.setattr(system.sec, 'download_document', download)
    return SimpleNamespace(worker=worker, system=system, read_html=read_html, sources=sources, downloads=downloads)


def operation(seeded, path, *, execute=False, guard=lambda: TARGET['task_definition']):
    return run(recovery.recover(seeded.worker.discovery, TARGET, path, execute=execute,
        check_stopped=guard, read_html=seeded.read_html))


def test_preview_queues_only_images_then_normal_restart_publishes(seeded, tmp_path):
    path = tmp_path / 'images.json'
    before = deepcopy(seeded.system.db.memory.tables)
    result = operation(seeded, path)
    assert result['image_count'] == 1
    assert seeded.system.db.memory.tables == before
    assert not seeded.downloads and not seeded.system.s3.objects
    operation(seeded, path, execute=True)
    after = deepcopy(seeded.system.db.memory.tables)
    operation(seeded, path, execute=True)
    assert seeded.system.db.memory.tables == after
    assert not seeded.downloads and not seeded.system.s3.objects
    async def finish():
        progress = await seeded.system.db.filings.get_checkpoint(ACCESSION)
        assert progress.enumerated_artifact_count == 1
        assert progress.selection_policy == 'earnings-acquisition-v2'
        assert len(progress.selected_artifact_ids) == 2
        summary = await RecoveryService(seeded.worker).run_pass()
        assert not summary.errors and not summary.terminal
        assert len(seeded.downloads) == len(seeded.system.s3.objects) == len(seeded.system.messages.events) == 1
        await RecoveryService(seeded.worker).run_pass()
        assert len(seeded.downloads) == 1
    run(finish())


def test_lost_create_ack_resumes_the_exact_saved_plan(seeded, tmp_path):
    path = tmp_path / 'images.json'
    operation(seeded, path)
    seeded.system.db.memory.lost.append('PutItem')
    with pytest.raises(ReadTimeoutError):
        operation(seeded, path, execute=True)
    operation(seeded, path, execute=True)
    assert not seeded.downloads


@pytest.mark.parametrize('change', ['source', 'task', 'manifest'])
def test_changed_inputs_refuse_writes(seeded, tmp_path, change):
    path = tmp_path / 'images.json'
    operation(seeded, path)
    guard = lambda: TARGET['task_definition']
    if change == 'source':
        # Same size and valid references; the stored-byte digest must catch this.
        seeded.sources['body'] = seeded.sources['body'].replace(b'1365', b'1364')
    elif change == 'task':
        guard = lambda: 'different:20'
    else:
        import json
        plan = json.loads(path.read_text())
        plan['additions'][0]['artifact']['filename'] = 'other.jpg'
        path.write_text(json.dumps(plan))
    before = deepcopy(seeded.system.db.memory.tables)
    with pytest.raises(RuntimeError):
        operation(seeded, path, execute=True, guard=guard)
    assert seeded.system.db.memory.tables == before and not seeded.downloads


def legacy_parent(seeded):
    """Model the completed production parents written before FilingReady existed."""
    row = seeded.system.db.memory.tables['filings'][(ACCESSION,)]
    for field in ('selected_artifact_ids', 'ready_snapshot_id', 'ready_published_at'):
        row.pop(field, None)
    return row


@pytest.mark.parametrize('missing_name', [False, True])
def test_legacy_preview_and_resume_preserve_first_facts_and_publish_full_snapshot(seeded, tmp_path, missing_name):
    import json
    row = legacy_parent(seeded)
    if missing_name:
        row.pop('company_name', None)
        run(seeded.system.db.companies.upsert(Company(ticker='AAPL', cik='320193', name='Apple')))
        seeded.worker.companies = seeded.system.db.companies
    path = tmp_path / 'legacy-images.json'
    before = deepcopy(seeded.system.db.memory.tables)
    assert operation(seeded, path)['image_count'] == 1
    assert seeded.system.db.memory.tables == before
    plan = json.loads(path.read_text())
    assert 'selected_artifact_ids' not in plan['parents'][ACCESSION]['checkpoint']
    # A lost image-create acknowledgment leaves the parent in its original
    # legacy form. Retrying must still validate that form against the same plan.
    seeded.system.db.memory.lost.append('PutItem')
    with pytest.raises(ReadTimeoutError):
        operation(seeded, path, execute=True)
    assert 'selected_artifact_ids' not in row
    operation(seeded, path, execute=True)
    after = deepcopy(seeded.system.db.memory.tables)
    operation(seeded, path, execute=True)
    assert seeded.system.db.memory.tables == after
    updated = after['filings'][(ACCESSION,)]
    for field in ('enumeration_completed_at', 'enumerated_artifact_count', 'selection_policy',
                  'selection_checked_at', 'discovered_at'):
        assert updated[field] == before['filings'][(ACCESSION,)][field]
    assert len(updated['selected_artifact_ids']) == 2 and updated['pending_work_kind'] == 'PUBLISH'
    original_key = (ACCESSION + '/Primary.htm',)
    assert after['artifacts'][original_key] == before['artifacts'][original_key]
    async def finish():
        summary = await RecoveryService(seeded.worker).run_pass()
        assert not summary.errors and not summary.terminal
        progress = await seeded.system.db.filings.get_checkpoint(ACCESSION)
        assert progress.ready_published_at and len(progress.selected_artifact_ids) == 2
        assert len(seeded.downloads) == 1
        await RecoveryService(seeded.worker).run_pass()
        assert len(seeded.downloads) == 1
    run(finish())


@pytest.mark.parametrize('change', ['count', 'missing_child', 'unpublished_child', 'unstored_child'])
def test_legacy_incomplete_or_changed_originals_refuse_preview_without_writes(seeded, tmp_path, change):
    row = legacy_parent(seeded)
    children = seeded.system.db.memory.tables['artifacts']
    key = (ACCESSION + '/Primary.htm',)
    if change == 'count':
        row['enumerated_artifact_count'] = 2
    elif change == 'missing_child':
        del children[key]
    elif change == 'unpublished_child':
        children[key].pop('published_at')
        children[key]['pending_work_kind'] = 'PUBLISH'
        children[key]['pending_work_sort'] = row['discovered_at'] + '/' + key[0]
    else:
        for field in ('published_at', 'stored_at', 's3_uri', 'content_type', 'size_bytes'):
            children[key].pop(field, None)
        children[key]['pending_work_kind'] = 'ACQUIRE'
        children[key]['pending_work_sort'] = row['discovered_at'] + '/' + key[0]
    before = deepcopy(seeded.system.db.memory.tables)
    path = tmp_path / 'refused.json'
    with pytest.raises(RuntimeError):
        operation(seeded, path)
    assert seeded.system.db.memory.tables == before
    assert not path.exists() and not seeded.downloads
