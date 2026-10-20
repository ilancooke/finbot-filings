"""Reviewed unresolved-parent recovery, followed by the normal ingestion path."""

from copy import deepcopy
from types import SimpleNamespace
import json

from botocore.exceptions import ReadTimeoutError
import pytest

from scripts import recover_unresolved_6k as recovery
from finbot_ingestion.ingestion.recovery_service import RecoveryService
from finbot_ingestion.selection.policy import AcquisitionPolicy, Decision, POLICY_VERSION
from finbot_ingestion.repositories.dynamodb.serialization import pending_sort
from tests.support.dynamodb import NOW, run, make_filing
from tests.support.six_k import wire_captures

TARGET = {'task_definition': 'reviewed:19', 'policy': POLICY_VERSION, 'image': 'reviewed-digest'}


@pytest.fixture
def seeded(system, monkeypatch):
    filings, packages, downloads = wire_captures(system, monkeypatch)
    worker = system.worker()
    worker.discovery.selection = AcquisitionPolicy.from_env({})
    async def seed():
        for filing in filings.values():
            await system.db.filings.create_if_absent(filing)
            await system.db.filings.record_selection(filing.accession_number,
                Decision('UNRESOLVED', 'unresolved_6_k'), NOW, policy_version=POLICY_VERSION)
        # An unrelated held filing must survive this recovery unchanged.
        unrelated = make_filing(form_type='6-K')
        await system.db.filings.create_if_absent(unrelated)
        await system.db.filings.record_selection(unrelated.accession_number,
            Decision('UNRESOLVED', 'unresolved_6_k'), NOW, policy_version=POLICY_VERSION)
    run(seed())
    return SimpleNamespace(system=system, worker=worker, packages=packages, downloads=downloads)


def operation(seeded, path, *, execute=False, guard=lambda: TARGET['task_definition']):
    return run(recovery.recover(seeded.worker.discovery, TARGET, path,
                               execute=execute, check_stopped=guard))


def test_preview_is_read_only_then_reopens_exactly_two_and_normal_worker_publishes(seeded, tmp_path):
    system = seeded.system
    path = tmp_path / 'preview.json'
    before = deepcopy(system.db.memory.tables)
    operation(seeded, path)
    assert system.db.memory.tables == before and not system.s3.objects and not system.messages.events
    result = operation(seeded, path, execute=True)
    assert result['queued_filings'] == sorted(recovery.TARGETS)
    after = deepcopy(system.db.memory.tables)
    operation(seeded, path, execute=True)
    assert system.db.memory.tables == after
    for accession in recovery.TARGETS:
        row = after['filings'][(accession,)]
        old = before['filings'][(accession,)]
        assert row['revision'] == old['revision'] + 1
        assert row['selection_decision'] == 'SELECT' and row['pending_work_kind'] == 'ENUMERATE'
    unrelated = set(before['filings']) - {(a,) for a in recovery.TARGETS}
    assert all(before['filings'][key] == after['filings'][key] for key in unrelated)
    assert not system.db.memory.tables['artifacts'] and not system.messages.events
    async def finish():
        summary = await RecoveryService(seeded.worker).run_pass()
        assert summary.errors == summary.terminal == 0
        assert len(system.s3.objects) == 4 and len(system.messages.events) == 2
        for accession in recovery.TARGETS:
            checkpoint = await system.db.filings.get_checkpoint(accession)
            assert checkpoint.enumeration_completed_at and checkpoint.ready_published_at
        worker = system.worker()
        worker.discovery.selection = seeded.worker.discovery.selection
        await RecoveryService(worker).run_pass()
        assert len(system.s3.objects) == 4 and len(system.messages.events) == 2
    run(finish())


def test_lost_acknowledgment_resumes_same_manifest(seeded, tmp_path):
    path = tmp_path / 'preview.json'
    operation(seeded, path)
    seeded.system.db.memory.lost.append('UpdateItem')
    with pytest.raises(ReadTimeoutError):
        operation(seeded, path, execute=True)
    operation(seeded, path, execute=True)
    assert all(seeded.system.db.memory.tables['filings'][(a,)]['selection_decision'] == 'SELECT'
               for a in recovery.TARGETS)


@pytest.mark.parametrize('change', ['source', 'parent', 'manifest', 'task', 'cutoff', 'terminal'])
def test_changed_source_parent_manifest_or_deployment_aborts_without_writes(seeded, tmp_path, change):
    path = tmp_path / 'preview.json'
    operation(seeded, path)
    accession = next(iter(recovery.TARGETS))
    row = seeded.system.db.memory.tables['filings'][(accession,)]
    guard = lambda: TARGET['task_definition']
    if change == 'source':
        from dataclasses import replace
        package = seeded.packages[accession]
        seeded.packages[accession] = replace(package, documents=tuple(
            replace(d, description='Earnings presentation') if d.document_type == 'EX-99.1' else d
            for d in package.documents))
    elif change == 'parent':
        row['revision'] += 1
    elif change == 'manifest':
        plan = json.loads(path.read_text())
        plan['filings'][accession]['selected'].pop()
        path.write_text(json.dumps(plan))
    elif change == 'task':
        guard = lambda: 'different:20'
    elif change == 'cutoff':
        seeded.worker.discovery.selection = AcquisitionPolicy.from_env({'LIVE_INGESTION_START_AT': '2027-01-01T00:00:00Z'})
    else:
        row.update(terminal_at=NOW.isoformat(), terminal_stage='ENUMERATE',
            terminal_error='error', terminal_error_type='Failure', enumeration_failures=1,
            failure_at=NOW.isoformat(), failure_stage='ENUMERATE', failure_error='error')
    before = deepcopy(seeded.system.db.memory.tables)
    with pytest.raises((RuntimeError, ValueError)):
        operation(seeded, path, execute=True, guard=guard)
    assert seeded.system.db.memory.tables == before


def test_revision_race_after_preview_does_not_overwrite_changes(seeded, tmp_path):
    path = tmp_path / 'preview.json'
    operation(seeded, path)
    def change(row):
        row['revision'] += 1
    seeded.system.db.memory.before_update = change
    with pytest.raises(RuntimeError, match='concurrent checkpoint'):
        operation(seeded, path, execute=True)
    assert all(row['selection_decision'] == 'UNRESOLVED'
               for row in seeded.system.db.memory.tables['filings'].values())


def test_preview_refuses_already_selected_parent(seeded, tmp_path):
    accession = next(iter(recovery.TARGETS))
    row = seeded.system.db.memory.tables['filings'][(accession,)]
    row.update(selection_decision='SELECT', selection_reason='earnings_6_k',
               pending_work_kind='ENUMERATE', pending_work_sort=pending_sort(NOW, accession))
    with pytest.raises(RuntimeError, match='expected unresolved'):
        operation(seeded, tmp_path / 'preview.json')
