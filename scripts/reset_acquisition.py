"""One-time production acquisition reset; default is a read-only inventory.

Never invoked by ingestion. Requires a stopped service and an explicit --execute.
Company configuration and calendar expectations/sync records are preserved.
"""
import argparse
from contextlib import ExitStack, closing
import json
from pathlib import Path
import time

import boto3
from botocore.config import Config

ACCOUNT = '559007813222'
REGION = 'us-east-1'
BUCKET = 'finbot-prod-state-artifacts82dd59a1-8gt63rcgmaua'
CLUSTER = SERVICE = 'finbot-prod'
TABLES = {'finbot-prod-filings': ['accession_number'], 'finbot-prod-artifacts': ['artifact_id']}
CALENDAR = 'finbot-prod-calendar'
SATISFACTION = '__event_satisfaction__'
QUEUE = f'https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/finbot-prod-failed-work'


def stopped(ecs):
    state = ecs.describe_services(cluster=CLUSTER, services=[SERVICE])
    if state.get('failures') or len(state.get('services', [])) != 1:
        raise RuntimeError('cannot identify service')
    service = state['services'][0]
    if any(service.get(k) != 0 for k in ('desiredCount', 'runningCount', 'pendingCount')):
        raise RuntimeError('ingestion must be stopped before inventory/reset')
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


def table_keys(db, table, keys):
    names = {f'#k{i}': key for i, key in enumerate(keys)}
    result = []
    for page in db.get_paginator('scan').paginate(TableName=table, ConsistentRead=True,
            ProjectionExpression=', '.join(names), ExpressionAttributeNames=names):
        result.extend(page.get('Items', []))
    return result


def inventory(s3, db):
    if s3.get_bucket_versioning(Bucket=BUCKET, ExpectedBucketOwner=ACCOUNT).get('Status') != 'Enabled':
        raise RuntimeError('expected versioned artifact bucket')
    versions = []
    for page in s3.get_paginator('list_object_versions').paginate(Bucket=BUCKET, ExpectedBucketOwner=ACCOUNT):
        versions.extend({'Key': v['Key'], 'VersionId': v['VersionId']}
                        for field in ('Versions', 'DeleteMarkers') for v in page.get(field, []))
    tables = {name: table_keys(db, name, keys) for name, keys in TABLES.items()}
    satisfaction = []
    for page in db.get_paginator('query').paginate(TableName=CALENDAR, ConsistentRead=True,
            KeyConditionExpression='expected_date = :partition',
            ExpressionAttributeValues={':partition': {'S': SATISFACTION}},
            ProjectionExpression='expected_date, cik'):
        satisfaction.extend(page.get('Items', []))
    tables[CALENDAR] = satisfaction
    return {'versions': versions, 'tables': tables}


def subset(current, planned):
    """Permit resuming partial deletion, but never delete unplanned new records."""
    return {json.dumps(v, sort_keys=True) for v in current} <= {json.dumps(v, sort_keys=True) for v in planned}


def reset(s3, db, sqs, ecs, planned, *, sleep=time.sleep):
    stopped(ecs)
    current = inventory(s3, db)
    if (not subset(current['versions'], planned['versions']) or
            any(not subset(rows, planned['tables'][table]) for table, rows in current['tables'].items())):
        raise RuntimeError('inventory contains unplanned records; preserve evidence and review a new inventory')
    for start in range(0, len(current['versions']), 1000):
        pending = current['versions'][start:start + 1000]
        for attempt in range(5):
            stopped(ecs)
            response = s3.delete_objects(Bucket=BUCKET, ExpectedBucketOwner=ACCOUNT,
                Delete={'Objects': pending, 'Quiet': True})
            errors = response.get('Errors', [])
            if not errors:
                break
            if (attempt == 4 or any(e.get('Code') not in {'InternalError', 'ServiceUnavailable', 'SlowDown'}
                                    for e in errors)):
                raise RuntimeError(f'S3 version deletion returned errors; ingestion remains stopped: {errors}')
            pending = [{'Key': e['Key'], 'VersionId': e['VersionId']} for e in errors]
            sleep(2 ** attempt)
        if start % 10000 == 0:
            print(json.dumps({'s3_versions_processed': min(start + 1000, len(current['versions']))}), flush=True)
    for table, rows in current['tables'].items():
        stopped(ecs)
        for start in range(0, len(rows), 25):
            pending = {table: [{'DeleteRequest': {'Key': k}} for k in rows[start:start + 25]]}
            for attempt in range(8):
                response = db.batch_write_item(RequestItems=pending)
                pending = response.get('UnprocessedItems', {})
                if not pending:
                    break
                sleep(min(2 ** attempt, 10))
            if pending:
                raise RuntimeError('DynamoDB deletion retry budget exhausted; ingestion remains stopped')
            if start % 10000 == 0:
                print(json.dumps({'table': table, 'records_processed': min(start + 25, len(rows))}), flush=True)
    stopped(ecs)
    sqs.purge_queue(QueueUrl=QUEUE)
    # SQS purge can take 60 seconds, including messages currently in flight.
    sleep(60)
    after = inventory(s3, db)
    attrs = sqs.get_queue_attributes(QueueUrl=QUEUE, AttributeNames=[
        'ApproximateNumberOfMessages', 'ApproximateNumberOfMessagesNotVisible', 'ApproximateNumberOfMessagesDelayed'])['Attributes']
    if after['versions'] or any(after['tables'].values()) or any(int(v) for v in attrs.values()):
        raise RuntimeError('reset verification failed; ingestion remains stopped')
    stopped(ecs)
    return {'s3_versions_remaining': 0, 'records_remaining': {t: 0 for t in after['tables']}, 'queue_attributes': attrs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', default='default')
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    config = Config(connect_timeout=5, read_timeout=10, retries={'mode': 'standard', 'total_max_attempts': 3})
    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    with ExitStack() as stack:
        clients = {name: stack.enter_context(closing(session.client(name, config=config)))
                   for name in ('sts', 'ecs', 's3', 'dynamodb', 'sqs')}
        if clients['sts'].get_caller_identity()['Account'] != ACCOUNT:
            raise RuntimeError('wrong AWS account')
        revision = stopped(clients['ecs'])
        if not args.execute:
            plan = {'account': ACCOUNT, 'region': REGION, 'bucket': BUCKET, 'queue': QUEUE,
                    'task_definition': revision, **inventory(clients['s3'], clients['dynamodb'])}
            with args.manifest.open('x') as out:
                json.dump(plan, out, indent=2)
            print(json.dumps({'s3_versions': len(plan['versions']),
                              'records': {t: len(r) for t, r in plan['tables'].items()}, 'manifest': str(args.manifest)}))
        else:
            plan = json.loads(args.manifest.read_text())
            if ((plan.get('account'), plan.get('region'), plan.get('bucket'), plan.get('queue'), plan.get('task_definition'))
                    != (ACCOUNT, REGION, BUCKET, QUEUE, revision) or set(plan['tables']) != {*TABLES, CALENDAR}):
                raise RuntimeError('manifest does not match the reviewed stopped production target')
            result = reset(clients['s3'], clients['dynamodb'], clients['sqs'], clients['ecs'], plan)
            output = args.manifest.with_suffix('.verified.json')
            with output.open('x') as out:
                json.dump(result, out, indent=2)
            print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
