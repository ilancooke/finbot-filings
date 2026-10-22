"""Preview/queue image dependencies for four reviewed filings; never download images.

Stop ingestion and pause consumers first. Preview reads canonical stored HTML and
fresh SEC package metadata. Execute conditionally adds only reviewed image rows
and commits complete membership; normal ingestion later downloads/publishes them.
Original HTML, prior snapshots, first enumeration facts and labels are preserved.
Legacy completed filings without selected membership are accepted only when the
current source selection matches their original count and every original child
is durably stored and published. Preview never repairs their checkpoint in place.
"""

import argparse
import asyncio
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

import boto3
from botocore.config import Config

from finbot_ingestion.aws_config import StorageConfig
from finbot_ingestion.config import IngestionConfig
from finbot_ingestion.domain import Artifact
from finbot_ingestion.execution import BlockingExecution
from finbot_ingestion.ingestion.config import WorkflowConfig
from finbot_ingestion.ingestion.discovery_service import DiscoveryService
from finbot_ingestion.ingestion.work_control import WorkControl
from finbot_ingestion.repositories.dynamodb import DynamoDBArtifactRepository, DynamoDBConfig, DynamoDBExecution, DynamoDBFilingRepository
from finbot_ingestion.repositories.dynamodb.serialization import record, restore, timestamp
from finbot_ingestion.repositories.package_checkpoint import assert_same_artifact
from finbot_ingestion.sec.client import SecClient
from finbot_ingestion.sec.rate_limiter import SECRateLimiter
from finbot_ingestion.selection.images import supporting_images, MAX_HTML_BYTES, MAX_SUPPORTING_IMAGES
from finbot_ingestion.selection.policy import AcquisitionPolicy, Decision, POLICY_VERSION, POLICY_UPGRADES

try:  # Direct script invocation and importable offline tests.
    from scripts.recover_earnings_attachments import REGION, production_environment, stopped, matches_preview
except ModuleNotFoundError:
    from recover_earnings_attachments import REGION, production_environment, stopped, matches_preview

TARGETS = {
    '0001628280-26-065813': ('ASML', '0000937966', '6-K'),
    '0000070858-26-000481': ('BAC', '0000070858', '8-K'),
    '0000731766-26-000255': ('UNH', '0000731766', '8-K'),
    '0000022444-26-000065': ('CMC', '0000022444', '10-K'),
}


async def inspect(discovery, target, at, read_html, *, resuming=False):
    parents, packages, additions, existing, source_hashes = {}, {}, [], {}, {}
    for accession, identity in TARGETS.items():
        filing = await discovery.filings.get(accession)
        progress = await discovery.filings.get_checkpoint(accession)
        if (filing is None or progress is None or (filing.ticker, filing.company_cik, filing.form_type) != identity
                or progress.enumeration_completed_at is None
                or progress.selection_decision != 'SELECT' or progress.terminal_at
                or (progress.selection_policy != POLICY_VERSION and
                    (progress.selection_policy, POLICY_VERSION) not in POLICY_UPGRADES)
                or (progress.ready_snapshot_id and not progress.ready_published_at)
                or discovery.selection.filing(filing).reason == 'before_live_cutoff'):
            raise RuntimeError(f'{accession}: incompatible completed filing; keep ingestion stopped')
        parents[accession] = {'filing': record(filing), 'checkpoint': record(progress)}
        package = await discovery.execution.call(discovery.sec.get_filing_index, filing=filing)
        admission, decisions, _ = await discovery.select_package(filing, package, progress)
        if not admission.selected or package.primary_document_name != progress.resolved_primary_document_name:
            raise RuntimeError(f'{accession}: changed document selection')
        documents = [doc for doc in package.documents if decisions[doc.filename].selected]
        expected_docs = {accession + '/' + doc.filename for doc in documents}
        legacy = progress.selected_artifact_ids is None
        if legacy and progress.enumerated_artifact_count != len(expected_docs):
            raise RuntimeError(f'{accession}: legacy document count differs from current selection')
        # These pre-FilingReady parents retain enumeration facts but no explicit
        # membership. Reconstruct only the exact original document selection;
        # each source-backed child is strongly read and verified below.
        old_docs = expected_docs if legacy else {key for key in progress.selected_artifact_ids
                    if not key.lower().endswith(('.jpg', '.jpeg', '.png', '.gif'))}
        if expected_docs != old_docs:
            raise RuntimeError(f'{accession}: document omissions require a separate recovery')
        candidates = {a.filename: a for a in package.artifacts(filing, discovered_at=at)}
        selected_images, total = {}, 0
        for doc in documents:
            child = await discovery.artifacts.get(accession + '/' + doc.filename)
            checkpoint = await discovery.artifacts.get_checkpoint(accession + '/' + doc.filename)
            if child is None or checkpoint is None or child.stored_at is None or checkpoint.terminal_at or checkpoint.selection_blocked:
                raise RuntimeError(f'{accession}: selected source is not durably stored')
            if legacy and child.published_at is None:
                raise RuntimeError(f'{accession}: legacy source has not completed publication')
            assert_same_artifact(child, candidates[doc.filename])
            existing[child.artifact_id] = {'artifact': record(child), 'checkpoint': record(checkpoint)}
            if not doc.filename.lower().endswith(('.htm', '.html')):
                continue
            body = await read_html(child)
            total += len(body)
            if len(body) != child.size_bytes or total > MAX_HTML_BYTES:
                raise RuntimeError(f'{accession}: stored HTML size disagrees or exceeds inspection bound')
            source_hashes[child.artifact_id] = hashlib.sha256(body).hexdigest()
            for name, decision in supporting_images(filing, package, body).items():
                if decision.selected:
                    selected_images[name] = decision
                    decisions[name] = decision
        if len(selected_images) > MAX_SUPPORTING_IMAGES:
            raise RuntimeError('supporting image count limit exceeded')
        image_ids = {accession + '/' + name for name in selected_images}
        if not set(progress.selected_artifact_ids or ()) <= expected_docs | image_ids:
            raise RuntimeError(f'{accession}: existing membership has unexpected images')
        for name, decision in selected_images.items():
            child = candidates[name]
            durable = await discovery.artifacts.get(child.artifact_id)
            checkpoint = await discovery.artifacts.get_checkpoint(child.artifact_id)
            if durable is not None and (not resuming or durable != child or checkpoint is None
                    or checkpoint.terminal_at or checkpoint.selection_blocked
                    or checkpoint.selection_policy not in (None, POLICY_VERSION)
                    or (checkpoint.selection_policy == POLICY_VERSION and
                        (checkpoint.selection_decision, checkpoint.selection_reason) != ('SELECT', decision.reason))):
                raise RuntimeError(f'{child.artifact_id}: existing image needs a separately reviewed plan')
            additions.append({'artifact': record(child), 'reason': decision.reason})
        packages[accession] = {'primary': package.primary_document_name,
            'documents': [dict(filename=d.filename, sec_url=d.sec_url, document_type=d.document_type,
                               description=d.description) for d in package.documents],
            'decisions': {name: dict(outcome=d.outcome, reason=d.reason) for name, d in decisions.items()}}
    return {'target': target, 'created_at': timestamp(at), 'parents': parents, 'packages': packages,
            'existing': existing, 'additions': additions, 'stored_html_sha256': source_hashes}


async def recover(discovery, target, manifest, *, execute=False, check_stopped, read_html):
    def guard():
        if check_stopped() != target['task_definition']:
            raise RuntimeError('production task definition changed; keep ingestion stopped')
    guard()
    if not execute:
        plan = await inspect(discovery, target, datetime.now(timezone.utc), read_html)
        guard()
        with manifest.open('x') as output:
            json.dump(plan, output, indent=2)
        return {'manifest': str(manifest), 'image_count': len(plan['additions']),
                'images': [item['artifact']['artifact_id'] for item in plan['additions']]}
    plan = json.loads(manifest.read_text())
    at = datetime.fromisoformat(plan['created_at'])
    observed = await inspect(discovery, target, at, read_html, resuming=True)
    if not matches_preview(observed, plan):
        raise RuntimeError('preview changed; no writes performed')
    guard()
    for addition in plan['additions']:
        child = restore(Artifact, addition['artifact'])
        guard()
        await discovery.artifacts.create_if_absent(child)
        if await discovery.artifacts.get(child.artifact_id) != child:
            raise RuntimeError('conflicting image record; keep ingestion stopped')
        guard()
        await discovery.artifacts.record_selection(child.artifact_id,
            Decision('SELECT', addition['reason']), at, policy_version=POLICY_VERSION)
    guard()
    if not matches_preview(await inspect(discovery, target, at, read_html, resuming=True), plan):
        raise RuntimeError('image checkpoint verification failed; keep ingestion stopped')
    for accession, parent in plan['parents'].items():
        guard()
        ids = tuple(sorted(accession + '/' + name for name, decision in
            plan['packages'][accession]['decisions'].items() if decision['outcome'] == 'SELECT'))
        checkpoint = parent['checkpoint']
        await discovery.filings.mark_enumerated(accession, artifact_ids=ids, artifact_count=len(ids),
            primary_document_name=checkpoint['resolved_primary_document_name'],
            completed_at=datetime.fromisoformat(checkpoint['enumeration_completed_at']))
    if not matches_preview(await inspect(discovery, target, at, read_html, resuming=True), plan):
        raise RuntimeError('membership verification failed; keep ingestion stopped')
    return {'queued_images': [item['artifact']['artifact_id'] for item in plan['additions']],
            'next': 'Restart the compatible ingestion release to acquire images and publish new complete snapshots.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', default='default')
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--private-config', type=Path, default=Path(__file__).resolve().parents[1] / 'infra/cdk/config.local.json')
    args = parser.parse_args()
    private = json.loads(args.private_config.read_text())
    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    config = Config(connect_timeout=5, read_timeout=10, retries={'mode': 'standard', 'total_max_attempts': 3})
    with ExitStack() as stack:
        sts, ecs, s3 = [stack.enter_context(closing(session.client(name, config=config))) for name in ('sts', 'ecs', 's3')]
        target, env = production_environment(sts, ecs, private.get('account'))
        bucket = StorageConfig.from_env(env).bucket
        sec_config = IngestionConfig.from_env({**env, 'SEC_USER_AGENT': private['sec_user_agent']})
        sec = stack.enter_context(SecClient(sec_config, limiter=SECRateLimiter(sec_config.sec_max_requests_per_second)))
        execution = stack.enter_context(BlockingExecution(max_workers=1, name='image-recovery'))
        db = stack.enter_context(DynamoDBExecution.from_config(DynamoDBConfig(REGION, 'finbot-prod-companies',
            'finbot-prod-calendar', 'finbot-prod-filings', 'finbot-prod-artifacts', max_workers=1), session=session))
        discovery = DiscoveryService(sec, execution, DynamoDBFilingRepository(db), DynamoDBArtifactRepository(db),
            WorkControl(WorkflowConfig()), selection=AcquisitionPolicy.from_env(env))
        def read_source(artifact):
            parts = urlsplit(artifact.s3_uri)
            if parts.scheme != 's3' or parts.netloc != bucket or artifact.size_bytes > 64 * 1024 * 1024:
                raise RuntimeError('unexpected stored source bucket or size')
            response = s3.get_object(Bucket=bucket, Key=unquote(parts.path[1:], errors='strict'))
            with closing(response['Body']) as body:
                if response['ContentLength'] != artifact.size_bytes:
                    raise RuntimeError('stored source size changed')
                return body.read(artifact.size_bytes + 1)
        async def read_html(artifact):
            return await execution.call(read_source, artifact=artifact)
        result = asyncio.run(recover(discovery, target, args.manifest, execute=args.execute,
            check_stopped=lambda: stopped(ecs), read_html=read_html))
        print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
