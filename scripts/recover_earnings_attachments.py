"""Recover exactly four omitted DAL/WFC/JNJ attachments; preview is read-only.

Pause delivery and stop/wait for ingestion before either mode. Execution only
queues canonical children; restarting ingestion downloads/publishes them normally.
"""
import argparse
import asyncio
from contextlib import ExitStack, closing
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import boto3
from botocore.config import Config

from finbot_ingestion.config import IngestionConfig
from finbot_ingestion.execution import BlockingExecution
from finbot_ingestion.ingestion.config import WorkflowConfig
from finbot_ingestion.ingestion.discovery_service import DiscoveryService
from finbot_ingestion.ingestion.work_control import WorkControl
from finbot_ingestion.repositories.dynamodb import (
    DynamoDBArtifactRepository, DynamoDBConfig, DynamoDBExecution, DynamoDBFilingRepository,
)
from finbot_ingestion.repositories.dynamodb.serialization import record, timestamp
from finbot_ingestion.repositories.package_checkpoint import assert_same_artifact
from finbot_ingestion.sec.client import SecClient
from finbot_ingestion.sec.rate_limiter import SECRateLimiter
from finbot_ingestion.sec.urls import document_url, filing_index_url
from finbot_ingestion.selection.policy import AcquisitionPolicy, POLICY_VERSION, POLICY_UPGRADES

REGION = 'us-east-1'
CLUSTER = SERVICE = 'finbot-prod'
# accession -> (canonical ticker, CIK, first enumeration count, filename -> type)
TARGETS = {
    '0000027904-26-000035': ('DAL', '0000027904', 1, {
        'deltaairlinesannouncessept.htm': 'EX-99.1'}),
    '0000072971-26-000318': ('WFC', '0000072971', 1, {
        'wfc3qer10-13x26ex991xrelea.htm': 'EX-99.1',
        'wfc3qer10-13x26ex992xsuppl.htm': 'EX-99.2'}),
    '0000200406-26-000189': ('JNJ', '0000200406', 2, {
        'a2026q3exhibit992.htm': 'EX-99.2'}),
}


def stopped(ecs):
    """Service counts alone can miss a task still completing its shutdown."""
    response = ecs.describe_services(cluster=CLUSTER, services=[SERVICE])
    if response.get('failures') or len(response.get('services', [])) != 1:
        raise RuntimeError('cannot identify production service')
    service = response['services'][0]
    if any(service.get(k) != 0 for k in ('desiredCount', 'runningCount', 'pendingCount')):
        raise RuntimeError('ingestion must be stopped before preview/execution')
    arns = set()
    for desired in ('RUNNING', 'STOPPED'):
        for page in ecs.get_paginator('list_tasks').paginate(
                cluster=CLUSTER, serviceName=SERVICE, desiredStatus=desired):
            arns.update(page.get('taskArns', []))
    ordered = sorted(arns)
    for start in range(0, len(ordered), 100):
        batch = ordered[start:start + 100]
        response = ecs.describe_tasks(cluster=CLUSTER, tasks=batch)
        tasks = response.get('tasks', [])
        if (response.get('failures') or {t['taskArn'] for t in tasks} != set(batch)
                or any(t['lastStatus'] != 'STOPPED' for t in tasks)):
            raise RuntimeError('cannot prove every observed task is STOPPED')
    return service['taskDefinition']


def production_environment(sts, ecs, account):
    if not isinstance(account, str) or len(account) != 12 or not account.isascii() or not account.isdigit():
        raise RuntimeError('private configuration must specify a 12-digit AWS account')
    if sts.get_caller_identity()['Account'] != account:
        raise RuntimeError('wrong AWS account')
    revision = stopped(ecs)
    if not revision.startswith(f'arn:aws:ecs:{REGION}:{account}:task-definition/finbot-prod-ingestion:'):
        raise RuntimeError('unexpected production task definition')
    definition = ecs.describe_task_definition(taskDefinition=revision)['taskDefinition']
    containers = definition['containerDefinitions']
    if len(containers) != 1 or containers[0]['name'] != 'ingestion':
        raise RuntimeError('unexpected ingestion container')
    image = containers[0]['image']
    if not image.startswith(f'{account}.dkr.ecr.{REGION}.amazonaws.com/finbot-prod-ingestion@sha256:'):
        raise RuntimeError('expected a pinned production image')
    env = {v['name']: v['value'] for v in containers[0].get('environment', [])}
    for key, value in {'AWS_REGION': REGION, 'FILINGS_TABLE': 'finbot-prod-filings',
                       'ARTIFACTS_TABLE': 'finbot-prod-artifacts'}.items():
        if env.get(key) != value:
            raise RuntimeError(f'unexpected production {key}')
    if 'LIVE_INGESTION_START_AT' not in env:
        raise RuntimeError('production cutoff is missing')
    return {'account': account, 'region': REGION, 'task_definition': revision,
            'image': image, 'policy': POLICY_VERSION, 'cutoff': env['LIVE_INGESTION_START_AT']}, env


async def inspect(discovery, target, at, *, resuming=False):
    """Strong canonical reads; source selection uses the normal ingestion path."""
    if POLICY_VERSION not in {'earnings-acquisition-v2', 'earnings-acquisition-v3'}:
        raise RuntimeError('this recovery requires the reviewed v2/v3 selector')
    parents, packages, additions, existing = {}, {}, [], {}
    for accession, (ticker, cik, count, names) in TARGETS.items():
        filing = await discovery.filings.get(accession)
        progress = await discovery.filings.get_checkpoint(accession)
        if (filing is None or progress is None
                or (filing.ticker, filing.company_cik, filing.form_type) != (ticker, cik, '8-K')
                or filing.filing_index_url != filing_index_url(cik, accession)
                or progress.enumeration_completed_at is None
                or progress.enumerated_artifact_count != count
                or progress.selection_decision != 'SELECT' or progress.terminal_at is not None
                or (progress.selection_policy != POLICY_VERSION
                    and (progress.selection_policy, POLICY_VERSION) not in POLICY_UPGRADES)):
            raise RuntimeError(f'{accession}: unexpected canonical completed filing')
        if discovery.selection.filing(filing).reason == 'before_live_cutoff':
            raise RuntimeError(f'{accession}: outside the deployed cutoff')
        parents[accession] = {'filing': record(filing), 'checkpoint': record(progress)}
        package = await discovery.execution.call(discovery.sec.get_filing_index, filing=filing)
        if package.primary_document_name != progress.resolved_primary_document_name:
            raise RuntimeError(f'{accession}: primary document changed')
        admission, decisions, primary = await discovery.select_package(filing, package, progress)
        if not admission.selected:
            raise RuntimeError(f'{accession}: source no longer admits an earnings filing')
        packages[accession] = {
            'primary': package.primary_document_name, 'items': list(package.items),
            'item_status': package.item_status,
            'documents': [asdict(d) for d in package.documents],
            'decisions': {name: asdict(decision) for name, decision in decisions.items()},
            'primary_routing_sha256': hashlib.sha256(primary.content).hexdigest() if primary else None,
        }
        candidates = {a.filename: a for a in package.artifacts(filing, discovered_at=at)}
        if any(name not in decisions or not decisions[name].selected
               or candidates[name].document_type != kind for name, kind in names.items()):
            raise RuntimeError(f'{accession}: expected attachments are not selected by the reviewed selector')
        for name, decision in decisions.items():
            if not decision.selected:
                continue
            child = candidates[name]
            if child.sec_url != document_url(cik, accession, name):
                raise RuntimeError(f'{accession}: unexpected SEC document URL')
            durable = await discovery.artifacts.get(child.artifact_id)
            checkpoint = await discovery.artifacts.get_checkpoint(child.artifact_id)
            if durable is not None:
                assert_same_artifact(durable, child)
                if durable.ticker != ticker or checkpoint is None:
                    raise RuntimeError(f'{child.artifact_id}: unexpected canonical artifact')
            if name in names:
                if durable is not None and (not resuming or durable != child
                        or checkpoint.terminal_at is not None or checkpoint.selection_blocked
                        or checkpoint.selection_policy not in (None, POLICY_VERSION)
                        or (checkpoint.selection_policy == POLICY_VERSION and
                            (checkpoint.selection_decision, checkpoint.selection_reason) !=
                            ('SELECT', decision.reason))):
                    raise RuntimeError(f'{child.artifact_id}: target already exists or is incompatible')
                additions.append({'artifact': record(child), 'reason': decision.reason})
            elif durable is None:
                raise RuntimeError(f'{child.artifact_id}: unexpected additional missing document')
            else:
                existing[child.artifact_id] = {'artifact': record(durable), 'checkpoint': record(checkpoint)}
    return {'target': target, 'created_at': timestamp(at), 'parents': parents,
            'packages': packages, 'existing': existing, 'additions': additions}


async def recover(discovery, target, manifest, *, execute=False, check_stopped):
    """Revalidate a saved preview; conditional creates allow interrupted resumes."""
    def guard():
        if check_stopped() != target['task_definition']:
            raise RuntimeError('production task definition changed')
    guard()
    if execute:
        planned = json.loads(manifest.read_text())
        at = datetime.fromisoformat(planned['created_at'])
        fresh = await inspect(discovery, target, at, resuming=True)
        if fresh != planned:
            raise RuntimeError('preview changed; no writes performed')
        guard()
        for addition in fresh['additions']:
            from finbot_ingestion.domain import Artifact
            from finbot_ingestion.repositories.dynamodb.serialization import restore
            from finbot_ingestion.selection.policy import Decision
            child = restore(Artifact, addition['artifact'])
            guard()
            await discovery.artifacts.create_if_absent(child)
            durable = await discovery.artifacts.get(child.artifact_id)
            checkpoint = await discovery.artifacts.get_checkpoint(child.artifact_id)
            if durable != child or checkpoint is None or checkpoint.terminal_at or checkpoint.selection_blocked:
                raise RuntimeError(f'{child.artifact_id}: conflicting recovery record')
            guard()
            await discovery.artifacts.record_selection(child.artifact_id,
                Decision('SELECT', addition['reason']), at, policy_version=POLICY_VERSION)
            checkpoint = await discovery.artifacts.get_checkpoint(child.artifact_id)
            if checkpoint is None or (checkpoint.selection_policy, checkpoint.selection_decision,
                    checkpoint.selection_reason) != (POLICY_VERSION, 'SELECT', addition['reason']):
                raise RuntimeError(f'{child.artifact_id}: selection checkpoint verification failed')
        guard()
        if await inspect(discovery, target, at, resuming=True) != planned:
            raise RuntimeError('recovery verification failed; keep ingestion stopped')
        return {'queued': [a['artifact']['artifact_id'] for a in fresh['additions']],
                'next': 'Restart ingestion to download and publish queued attachments.'}
    fresh = await inspect(discovery, target, datetime.now(timezone.utc))
    guard()
    with manifest.open('x') as out:
        json.dump(fresh, out, indent=2)
    return {'manifest': str(manifest), 'additions': fresh['additions']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', default='default')
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--private-config', type=Path,
        default=Path(__file__).resolve().parents[1] / 'infra/cdk/config.local.json')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    private = json.loads(args.private_config.read_text())
    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    config = Config(connect_timeout=5, read_timeout=10,
                    retries={'mode': 'standard', 'total_max_attempts': 3})
    with ExitStack() as stack:
        sts, ecs = [stack.enter_context(closing(session.client(name, config=config)))
                    for name in ('sts', 'ecs')]
        target, env = production_environment(sts, ecs, private.get('account'))
        sec_config = IngestionConfig.from_env({**env, 'SEC_USER_AGENT': private['sec_user_agent']})
        sec = stack.enter_context(SecClient(sec_config,
            limiter=SECRateLimiter(sec_config.sec_max_requests_per_second)))
        execution = stack.enter_context(BlockingExecution(max_workers=1, name='sec'))
        db_config = DynamoDBConfig(REGION, 'finbot-prod-companies', 'finbot-prod-calendar',
                                   'finbot-prod-filings', 'finbot-prod-artifacts', max_workers=1)
        db = stack.enter_context(DynamoDBExecution.from_config(db_config, session=session))
        discovery = DiscoveryService(sec, execution, DynamoDBFilingRepository(db),
            DynamoDBArtifactRepository(db), WorkControl(WorkflowConfig()),
            selection=AcquisitionPolicy.from_env(env))
        result = asyncio.run(recover(discovery, target, args.manifest, execute=args.execute,
                                     check_stopped=lambda: stopped(ecs)))
        print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
