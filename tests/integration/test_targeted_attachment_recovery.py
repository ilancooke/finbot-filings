"""Operator preview/resume and normal worker replay through real AWS adapters."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import json

from botocore.exceptions import ReadTimeoutError
from scripts import recover_earnings_attachments as recovery
import pytest

from finbot_ingestion.domain import Filing
from finbot_ingestion.ingestion.recovery_service import RecoveryService
from finbot_ingestion.repositories.package_checkpoint import PackageCheckpoint
from finbot_ingestion.sec.client import DownloadedDocument
from finbot_ingestion.sec.filing_index import FilingIndex, PackageDocument
from finbot_ingestion.sec.urls import document_url, filing_index_url
from finbot_ingestion.selection.policy import AcquisitionPolicy, Decision, POLICY_VERSION

from tests.support.dynamodb import NOW
from tests.support.ingestion import run


ACCOUNT = '123456789012'
TARGET = {'account': ACCOUNT, 'region': recovery.REGION,
          'task_definition': 'reviewed:19', 'image': 'reviewed-digest',
          'policy': POLICY_VERSION, 'cutoff': '2026-10-06T04:00:00Z'}


class ECS:
    def __init__(self, count=0, status='STOPPED'):
        self.count, self.status = count, status

    def describe_services(self, **kwargs):
        return {'services': [{'desiredCount': self.count, 'runningCount': self.count,
                             'pendingCount': 0, 'taskDefinition': 'new'}]}

    def get_paginator(self, name):
        assert name == 'list_tasks'
        return SimpleNamespace(paginate=lambda **kwargs: iter([{'taskArns': ['previous']}]))

    def describe_tasks(self, **kwargs):
        return {'tasks': [{'taskArn': 'previous', 'lastStatus': self.status}]}


@pytest.fixture
def seeded(system, monkeypatch):
    worker = system.worker()
    worker.discovery.selection = AcquisitionPolicy.from_env({})
    packages, primaries = {}, {}
    async def seed():
        for accession, (ticker, cik, count, names) in recovery.TARGETS.items():
            filing = Filing(accession_number=accession, company_cik=cik, ticker=ticker,
                form_type='8-K', filed_at=NOW, discovered_at=NOW, company_name=ticker + ' company',
                filing_index_url=filing_index_url(cik, accession), primary_document_name='primary.htm')
            rows = [PackageDocument('primary.htm', document_url(cik, accession, 'primary.htm'), '8-K')]
            if ticker == 'JNJ':
                rows.append(PackageDocument('existing-release.htm',
                    document_url(cik, accession, 'existing-release.htm'), 'EX-99.1', 'Earnings release'))
            descriptions = {
                'DAL': ['EX-99.1'],
                'WFC': ['News Release dated October 13', '3Q26 Quarterly Supplement'],
                'JNJ': ['EX-99.2'],
            }[ticker]
            rows += [PackageDocument(name, document_url(cik, accession, name), kind, description)
                     for (name, kind), description in zip(names.items(), descriptions, strict=True)]
            rows.append(PackageDocument('deck.htm', document_url(cik, accession, 'deck.htm'),
                                        'EX-99.3', 'Earnings presentation'))
            package = FilingIndex(tuple(rows), 'primary.htm', ('2.02', '9.01'), 'known')
            packages[accession] = package
            primary = '<div>Item 2.02 Results</div><p>Exhibits 99.1 and 99.2.</p><div>Item 9.01 Exhibits</div>'
            if ticker == 'DAL':
                primary += f'<a href="{next(iter(names))}">Press release announcing quarterly financial results</a>'
            elif ticker == 'JNJ':
                primary += '<table><tr><td>99.2</td><td><a href="a2026q3exhibit992.htm">'
                primary += 'Supplementary Sales Data and Condensed Consolidated Statement of Earnings</a></td></tr></table>'
            primaries[accession] = primary.encode()
            await system.db.filings.create_if_absent(filing)
            await system.db.filings.record_selection(accession, Decision('SELECT', 'earnings_item_2_02'), NOW,
                                                     policy_version='earnings-acquisition-v1')
            old = package.artifacts(filing, discovered_at=NOW)[:count]
            await PackageCheckpoint(system.db.filings, system.db.artifacts).persist(filing, old,
                primary_document_name='primary.htm', completed_at=NOW)
            for child in old:
                await system.db.artifacts.record_selection(child.artifact_id, Decision('SELECT', 'primary_report'
                    if child.filename == 'primary.htm' else 'earnings_release'), NOW,
                    policy_version='earnings-acquisition-v1')
                await system.db.artifacts.mark_stored(child.artifact_id, 's3://existing/old/' + child.filename,
                    NOW, 'text/html', 3)
                await system.db.artifacts.mark_published(child.artifact_id, NOW)
    run(seed())
    downloads = []
    def index(filing):
        return packages[filing.accession_number]
    def download(url, *, max_bytes, context=None):
        accession = context['accession_number']
        name = url.rsplit('/', 1)[-1]
        assert name == 'primary.htm' or name in recovery.TARGETS[accession][3]
        downloads.append((accession, name))
        content = primaries[accession] if name == 'primary.htm' else b'original earnings attachment'
        return DownloadedDocument(content, 'text/html', url)
    monkeypatch.setattr(system.sec, 'get_filing_index', index)
    monkeypatch.setattr(system.sec, 'download_document', download)
    return SimpleNamespace(system=system, discovery=worker.discovery, worker=worker,
                           packages=packages, downloads=downloads)


def operation(seeded, path, *, execute=False, guard=lambda: TARGET['task_definition']):
    return run(recovery.recover(seeded.discovery, TARGET, path, execute=execute, check_stopped=guard))


def mutations(system):
    return [op for op, _ in system.db.memory.calls if op != 'GetItem']


def test_preview_is_read_only_and_lists_exact_four(seeded, tmp_path):
    memory = seeded.system.db.memory
    before = deepcopy(memory.tables)
    memory.calls.clear()
    path = tmp_path / 'preview.json'
    result = operation(seeded, path)
    assert len(result['additions']) == 4
    assert {a['artifact']['ticker'] for a in result['additions']} == {'DAL', 'WFC', 'JNJ'}
    assert [a['reason'] for a in result['additions']] == [
        'earnings_release', 'earnings_release', 'financial_supplement', 'financial_supplement']
    assert memory.tables == before and not mutations(seeded.system)
    assert not seeded.system.s3.objects and not seeded.system.messages.events
    assert all(name == 'primary.htm' for _, name in seeded.downloads)
    with pytest.raises(FileExistsError):
        operation(seeded, path)


def test_execute_preserves_history_and_normal_worker_downloads_once(seeded, tmp_path):
    system = seeded.system
    before = deepcopy(system.db.memory.tables)
    path = tmp_path / 'preview.json'
    operation(seeded, path)
    result = operation(seeded, path, execute=True)
    assert len(result['queued']) == 4
    for key, old in before['filings'].items():
        current = system.db.memory.tables['filings'][key]
        assert current['enumeration_completed_at'] == old['enumeration_completed_at']
        assert current['enumerated_artifact_count'] == old['enumerated_artifact_count']
        assert len(current['selected_artifact_ids']) > len(old['selected_artifact_ids'])
    assert all(system.db.memory.tables['artifacts'][key] == row for key, row in before['artifacts'].items())
    assert system.db.memory.tables['calendar'] == before['calendar']
    assert system.db.memory.tables['companies'] == before['companies']
    assert not system.s3.objects and not system.messages.events
    after = deepcopy(system.db.memory.tables)
    operation(seeded, path, execute=True)
    assert system.db.memory.tables == after
    async def finish():
        assert len((await system.db.artifacts.list_pending('ACQUIRE')).items) == 4
        summary = await RecoveryService(seeded.worker).run_pass()
        assert summary.complete >= 4 and summary.errors == summary.terminal == 0
        assert len(system.s3.objects) == 4 and len(system.messages.events) == 3
        events = [json.loads(params['Message']) for op, params in system.messages.events if op == 'Publish']
        assert sum(len(event['artifacts']) for event in events) == 8
        for identity in result['queued']:
            child = await system.db.artifacts.get(identity)
            assert child.stored_at is not None and child.published_at is not None
        for accession in recovery.TARGETS:
            progress = await system.db.filings.get_checkpoint(accession)
            assert progress.enumerated_artifact_count == recovery.TARGETS[accession][2]
            assert progress.enumeration_completed_at == NOW
        restarted = system.worker()
        restarted.discovery.selection = seeded.discovery.selection
        await RecoveryService(restarted).run_pass()
        assert len(system.s3.objects) == 4 and len(system.messages.events) == 3
        events = [json.loads(params['Message']) for op, params in system.messages.events if op == 'Publish']
        assert sum(len(event['artifacts']) for event in events) == 8
    run(finish())
    assert all(system.db.memory.tables['artifacts'][key] == row for key, row in before['artifacts'].items())


@pytest.mark.parametrize('op', ['PutItem', 'UpdateItem'])
def test_lost_acknowledgment_resumes_same_manifest_without_duplicates(seeded, tmp_path, op):
    path = tmp_path / 'preview.json'
    operation(seeded, path)
    seeded.system.db.memory.lost.append(op)
    with pytest.raises(ReadTimeoutError):
        operation(seeded, path, execute=True)
    assert 4 < len(seeded.system.db.memory.tables['artifacts']) < 8
    result = operation(seeded, path, execute=True)
    assert len(result['queued']) == 4
    assert len(seeded.system.db.memory.tables['artifacts']) == 8


def test_interruption_after_first_completed_addition_resumes(seeded, tmp_path):
    path = tmp_path / 'preview.json'
    result = operation(seeded, path)
    memory = seeded.system.db.memory
    memory.fail_put = result['additions'][1]['artifact']['artifact_id']
    with pytest.raises(seeded.system.db.client.exceptions.InternalServerError):
        operation(seeded, path, execute=True)
    assert len(memory.tables['artifacts']) == 5
    first = deepcopy(memory.tables['artifacts'][(result['additions'][0]['artifact']['artifact_id'],)])
    memory.fail_put = None
    operation(seeded, path, execute=True)
    assert len(memory.tables['artifacts']) == 8
    assert memory.tables['artifacts'][(first['artifact_id'],)] == first


@pytest.mark.parametrize('change', ['cutoff', 'blocked', 'unknown_policy', 'conflicting_items', 'primary'])
def test_preview_rejects_ineligible_parent_or_source(seeded, tmp_path, change):
    accession = next(iter(recovery.TARGETS))
    row = seeded.system.db.memory.tables['filings'][(accession,)]
    if change == 'cutoff':
        seeded.discovery.selection = AcquisitionPolicy.from_env({'LIVE_INGESTION_START_AT': '2027-01-01T00:00:00Z'})
    elif change == 'blocked':
        row['selection_decision'] = 'UNRESOLVED'
    elif change == 'unknown_policy':
        row['selection_policy'] = 'unknown-policy'
    elif change == 'conflicting_items':
        seeded.packages[accession] = replace(seeded.packages[accession], items=('7.01',))
    else:
        seeded.packages[accession] = replace(seeded.packages[accession], primary_document_name='other.htm')
    seeded.system.db.memory.calls.clear()
    with pytest.raises(RuntimeError):
        operation(seeded, tmp_path / 'preview.json')
    assert not mutations(seeded.system)


@pytest.mark.parametrize('change', ['source', 'parent', 'manifest', 'task', 'blocked'])
def test_execute_rejects_changed_plan_before_writes(seeded, tmp_path, change):
    path = tmp_path / 'preview.json'
    result = operation(seeded, path)
    accession = next(iter(recovery.TARGETS))
    if change == 'source':
        package = seeded.packages[accession]
        seeded.packages[accession] = replace(package, documents=tuple(d for d in package.documents
            if d.filename != 'deltaairlinesannouncessept.htm'))
    elif change == 'parent':
        seeded.system.db.memory.tables['filings'][(accession,)]['enumerated_artifact_count'] = 2
    elif change == 'manifest':
        plan = json.loads(path.read_text())
        plan['additions'].pop()
        path.write_text(json.dumps(plan))
    elif change == 'blocked':
        from finbot_ingestion.domain import Artifact
        from finbot_ingestion.repositories.dynamodb.serialization import restore
        child = restore(Artifact, result['additions'][0]['artifact'])
        async def block():
            await seeded.discovery.artifacts.create_if_absent(child)
            await seeded.discovery.artifacts.record_selection(child.artifact_id,
                Decision('EXCLUDE', 'presentation'), NOW, policy_version=POLICY_VERSION)
        run(block())
    before = deepcopy(seeded.system.db.memory.tables)
    seeded.system.db.memory.calls.clear()
    guard = (lambda: 'different:20') if change == 'task' else (lambda: TARGET['task_definition'])
    with pytest.raises(RuntimeError):
        operation(seeded, path, execute=True, guard=guard)
    assert seeded.system.db.memory.tables == before and not mutations(seeded.system)


def test_preview_rejects_unplanned_selected_addition(seeded, tmp_path):
    accession = next(iter(recovery.TARGETS))
    package = seeded.packages[accession]
    seeded.packages[accession] = replace(package, documents=package.documents + (
        PackageDocument('other-release.htm', document_url(recovery.TARGETS[accession][1], accession,
            'other-release.htm'), 'EX-99.4', 'Earnings release'),))
    seeded.system.db.memory.calls.clear()
    with pytest.raises(RuntimeError, match='unexpected additional missing'):
        operation(seeded, tmp_path / 'preview.json')
    assert not mutations(seeded.system)


@pytest.mark.parametrize('count,status', [(1, 'RUNNING'), (0, 'STOPPING')])
def test_stopped_guard_rejects_running_and_stopping_tasks(count, status):
    with pytest.raises(RuntimeError, match='stopped|STOPPED'):
        recovery.stopped(ECS(count=count, status=status))


def test_stopped_guard_accepts_fully_stopped_tasks():
    assert recovery.stopped(ECS()) == 'new'


def test_wrong_account_stops_before_ecs_or_sec():
    sts = SimpleNamespace(get_caller_identity=lambda: {'Account': 'wrong'})
    with pytest.raises(RuntimeError, match='wrong AWS account'):
        recovery.production_environment(sts, None, ACCOUNT)


@pytest.mark.parametrize('change', [None, 'table', 'image', 'cutoff', 'revision'])
def test_production_target_validation(change):
    revision = f'arn:aws:ecs:{recovery.REGION}:{ACCOUNT}:task-definition/finbot-prod-ingestion:19'
    env = {'AWS_REGION': recovery.REGION, 'FILINGS_TABLE': 'finbot-prod-filings',
           'ARTIFACTS_TABLE': 'finbot-prod-artifacts', 'LIVE_INGESTION_START_AT': TARGET['cutoff']}
    image = f'{ACCOUNT}.dkr.ecr.{recovery.REGION}.amazonaws.com/finbot-prod-ingestion@sha256:' + 'a' * 64
    if change == 'table':
        env['ARTIFACTS_TABLE'] = 'other-artifacts'
    elif change == 'image':
        image = 'unreviewed:latest'
    elif change == 'cutoff':
        env.pop('LIVE_INGESTION_START_AT')
    elif change == 'revision':
        revision = 'other-family:1'
    definition = {'containerDefinitions': [{'name': 'ingestion', 'image': image,
        'environment': [{'name': k, 'value': v} for k, v in env.items()]}]}
    ecs = ECS()
    ecs.describe_services = lambda **kwargs: {'services': [{'desiredCount': 0, 'runningCount': 0,
        'pendingCount': 0, 'taskDefinition': revision}]}
    ecs.describe_task_definition = lambda **kwargs: {'taskDefinition': definition}
    sts = SimpleNamespace(get_caller_identity=lambda: {'Account': ACCOUNT})
    if change:
        with pytest.raises(RuntimeError):
            recovery.production_environment(sts, ecs, ACCOUNT)
    else:
        target, returned = recovery.production_environment(sts, ecs, ACCOUNT)
        assert target['image'] == image and target['task_definition'] == revision
        assert returned == env
