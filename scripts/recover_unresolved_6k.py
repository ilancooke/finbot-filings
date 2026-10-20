"""Preview/reopen only the reviewed October 15 ERIC/TSM earnings filings.

Run as: python -m scripts.recover_unresolved_6k --manifest NEW_PATH
Pause delivery and stop/wait for ingestion before preview or execution. Execution
only queues parent enumeration; the reviewed deployed image acquires/publishes.
"""

from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json

from finbot_ingestion.repositories.dynamodb.serialization import integer, pending_sort, timestamp
from finbot_ingestion.sec.urls import filing_index_url
from finbot_ingestion.selection.policy import POLICY_VERSION
from .recover_earnings_attachments import main as operator_main

# Exact reviewed parent and complete selected artifact membership, not filename
# classification rules: the normal selector must independently reproduce these.
TARGETS = {
    '0001628280-26-065962': ('ERIC', '0000717826', 'a2610156-kq326.htm', 'a2026q3forfiling.htm'),
    '0001046179-26-000726': ('TSM', '0001046179', 'tsm-20261015x6k.htm', 'a3q26e_withguidancexfinal.htm'),
}


def _json_value(value):
    return json.loads(json.dumps(value, default=lambda number: integer(number, 'number')
                                if isinstance(number, Decimal) else _unsupported(number)))


def _unsupported(value):
    raise TypeError(f'unsupported manifest value: {type(value).__name__}')


def _reopened(row, at):
    return {**row, 'revision': integer(row['revision'], 'revision') + 1,
            'selection_decision': 'SELECT', 'selection_reason': 'earnings_6_k',
            'selection_checked_at': timestamp(at), 'pending_work_kind': 'ENUMERATE',
            'pending_work_sort': pending_sort(datetime.fromisoformat(row['discovered_at']), row['accession_number'])}


async def inspect(discovery, target, at, planned=None):
    if POLICY_VERSION != 'earnings-acquisition-v3' or target.get('policy') != POLICY_VERSION:
        raise RuntimeError('recovery requires the reviewed v3 image/selector')
    result = {'target': target, 'created_at': timestamp(at), 'filings': {}}
    for accession, (ticker, cik, primary, release) in TARGETS.items():
        repo = discovery.filings
        row = await repo._get(accession)  # Strong complete-row read, including revision.
        if row is None:
            raise RuntimeError(f'{accession}: missing filing')
        filing = repo._validate(row)
        row = _json_value(row)
        progress = await repo.get_checkpoint(accession)
        if (filing.ticker, filing.company_cik, filing.form_type, filing.primary_document_name,
                filing.filing_index_url) != (ticker, cik, '6-K', primary, filing_index_url(cik, accession)):
            raise RuntimeError(f'{accession}: unexpected canonical identity')
        if (progress is None or progress.terminal_at or progress.enumeration_completed_at
                or progress.ready_snapshot_id or progress.selection_policy != POLICY_VERSION
                or discovery.selection.filing(filing).reason == 'before_live_cutoff'):
            raise RuntimeError(f'{accession}: not an eligible unresolved parent')
        if planned is None:
            if (progress.selection_decision, progress.selection_reason) != ('UNRESOLVED', 'unresolved_6_k'):
                raise RuntimeError(f'{accession}: expected unresolved_6_k')
            original = row
        else:
            original = planned['filings'][accession]['before']
            if (original.get('selection_policy'), original.get('selection_decision'),
                    original.get('selection_reason')) != (POLICY_VERSION, 'UNRESOLVED', 'unresolved_6_k'):
                raise RuntimeError(f'{accession}: preview must describe unresolved_6_k')
            if row not in (original, _reopened(original, at)):
                raise RuntimeError(f'{accession}: checkpoint changed since preview')
        # The service is stopped. Reject unexpected pre-existing children, rather
        # than interpreting this operation as attachment/snapshot repair.
        children = await discovery.artifacts.list_for_filing(accession)
        if children.items or children.next_token is not None:
            raise RuntimeError(f'{accession}: artifacts already exist')
        for name in (primary, release):
            if await discovery.artifacts.get(accession + '/' + name) is not None:
                raise RuntimeError(f'{accession}: artifact exists in a strong read')
        package = await discovery.execution.call(discovery.sec.get_filing_index, filing=filing)
        downloaded = {}
        admission, decisions, cover = await discovery.select_package(
            filing, package, progress, routing_downloads=downloaded)
        selected = sorted(name for name, decision in decisions.items() if decision.selected)
        if not admission.selected or selected != sorted([primary, release]):
            raise RuntimeError(f'{accession}: source does not select exactly the reviewed artifacts')
        if cover is None or package.primary_document_name != primary:
            raise RuntimeError(f'{accession}: primary routing evidence missing/changed')
        downloaded[primary] = cover
        result['filings'][accession] = {
            'before': original, 'after': _reopened(original, at),
            'documents': [asdict(document) for document in package.documents],
            'decisions': {name: asdict(decision) for name, decision in decisions.items()},
            'routing_sha256': {name: hashlib.sha256(value.content).hexdigest()
                               for name, value in sorted(downloaded.items())},
            'selected': selected,
        }
    return result


async def recover(discovery, target, manifest, *, execute=False, check_stopped):
    def guard():
        if check_stopped() != target['task_definition']:
            raise RuntimeError('production task definition changed; keep ingestion stopped')
    guard()
    if not execute:
        preview = await inspect(discovery, target, datetime.now(timezone.utc))
        guard()
        with manifest.open('x') as output:
            json.dump(preview, output, indent=2)
        return {'manifest': str(manifest), 'filings': {
            accession: entry['selected'] for accession, entry in preview['filings'].items()}}
    planned = json.loads(manifest.read_text())
    if planned.get('target') != target or set(planned.get('filings', {})) != set(TARGETS):
        raise RuntimeError('manifest has a different deployment or filing scope')
    at = datetime.fromisoformat(planned['created_at'])
    if await inspect(discovery, target, at, planned) != planned:
        raise RuntimeError('source or recovery plan changed; no writes performed')
    for accession in TARGETS:
        guard()
        entry = planned['filings'][accession]
        def decide(row):
            if _json_value(row) == entry['after']:
                return None  # Interrupted/lost-ack execution of the same manifest.
            if _json_value(row) != entry['before']:
                raise RuntimeError(f'{accession}: concurrent checkpoint change')
            return {name: value for name, value in entry['after'].items()
                    if name != 'revision' and entry['before'].get(name) != value}, ()
        repo = discovery.filings
        await repo._update(accession, decide, repo._validate)
    guard()
    if await inspect(discovery, target, at, planned) != planned:
        raise RuntimeError('post-write verification failed; keep ingestion stopped')
    return {'queued_filings': sorted(TARGETS),
            'next': 'Restart the reviewed image to enumerate, acquire and publish the selected artifacts.'}


if __name__ == '__main__':
    raise SystemExit(operator_main(description=__doc__, operation=recover))
