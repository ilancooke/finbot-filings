"""Offline reset tests cover scope, version deletion and interrupted batches."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from scripts import reset_acquisition as reset


class Paginator:
    def __init__(self, pages):
        self.pages = pages
    def paginate(self, **kwargs):
        return iter(self.pages(kwargs))


class ECS:
    def __init__(self, count=0, status='STOPPED'):
        self.count, self.status = count, status
    def describe_services(self, **kwargs):
        return {'services': [{'desiredCount': self.count, 'runningCount': self.count,
                             'pendingCount': 0, 'taskDefinition': 'new'}]}
    def get_paginator(self, name):
        return Paginator(lambda k: [{'taskArns': ['previous']}])
    def describe_tasks(self, **kwargs):
        return {'tasks': [{'taskArn': 'previous', 'lastStatus': self.status}]}


class S3:
    def __init__(self):
        self.versions = [{'Key': 'old.htm', 'VersionId': 'bytes'}, {'Key': 'old.htm', 'VersionId': 'marker'}]
        self.deleted = []
        self.errors = False
    def get_bucket_versioning(self, **kwargs):
        assert kwargs['ExpectedBucketOwner'] == reset.ACCOUNT
        return {'Status': 'Enabled'}
    def get_paginator(self, name):
        return Paginator(lambda k: [{'Versions': self.versions[:1]}, {'DeleteMarkers': self.versions[1:]}])
    def delete_objects(self, **kwargs):
        if self.errors:
            return {'Errors': [{'Code': 'AccessDenied'}]}
        objects = kwargs['Delete']['Objects']
        self.deleted.extend(objects)
        self.versions = [v for v in self.versions if v not in objects]
        return {}


class DB:
    def __init__(self):
        self.rows = {t: [{k[0]: {'S': 'old'}}] for t, k in reset.TABLES.items()}
        self.rows[reset.CALENDAR] = [{'expected_date': {'S': reset.SATISFACTION}, 'cik': {'S': 'event'}}]
        self.preserved = [{'expected_date': {'S': '2026-10-09'}, 'cik': {'S': '1'}}]
        self.retry = True
        self.deleted = []
    def get_paginator(self, name):
        def pages(k):
            if name == 'query':
                assert k['ExpressionAttributeValues'] == {':partition': {'S': reset.SATISFACTION}}
            assert k['ConsistentRead'] is True
            return [{'Items': []}, {'Items': deepcopy(self.rows[k['TableName']])}]
        return Paginator(pages)
    def batch_write_item(self, RequestItems):
        if self.retry:
            self.retry = False
            return {'UnprocessedItems': RequestItems}
        for table, requests in RequestItems.items():
            for request in requests:
                key = request['DeleteRequest']['Key']
                self.rows[table].remove(key)
                self.deleted.append((table, key))
        return {}


class SQS:
    def __init__(self):
        self.purged = False
    def purge_queue(self, QueueUrl):
        assert QueueUrl == reset.QUEUE
        self.purged = True
    def get_queue_attributes(self, **kwargs):
        return {'Attributes': {'ApproximateNumberOfMessages': '0', 'ApproximateNumberOfMessagesNotVisible': '0',
                               'ApproximateNumberOfMessagesDelayed': '0'}}


@pytest.fixture
def system():
    return SimpleNamespace(s3=S3(), db=DB(), ecs=ECS(), sqs=SQS())


def test_inventory_reads_all_pages_and_does_not_delete(system):
    plan = reset.inventory(system.s3, system.db)
    assert len(plan['versions']) == 2
    assert all(len(rows) == 1 for rows in plan['tables'].values())
    assert not system.s3.deleted and not system.db.deleted and not system.sqs.purged


@pytest.mark.parametrize('count,status', [(1, 'RUNNING'), (0, 'STOPPING')])
def test_reset_refuses_live_or_stopping_tasks(system, count, status):
    plan = reset.inventory(system.s3, system.db)
    system.ecs = ECS(count=count, status=status)
    with pytest.raises(RuntimeError, match='stopped|STOPPED'):
        reset.reset(system.s3, system.db, system.sqs, system.ecs, plan, sleep=lambda _: None)
    assert not system.s3.deleted and not system.db.deleted and not system.sqs.purged


def test_reset_retries_unprocessed_and_preserves_calendar_configuration(system):
    plan = reset.inventory(system.s3, system.db)
    sleeps = []
    result = reset.reset(system.s3, system.db, system.sqs, system.ecs, plan, sleep=sleeps.append)
    assert result['s3_versions_remaining'] == 0
    assert system.s3.deleted == plan['versions']
    assert all(not rows for rows in system.db.rows.values())
    assert system.db.preserved and system.sqs.purged
    assert sleeps == [1, 60]


def test_reset_refuses_unplanned_objects_before_any_deletion(system):
    plan = reset.inventory(system.s3, system.db)
    system.s3.versions.append({'Key': 'new.htm', 'VersionId': 'new'})
    with pytest.raises(RuntimeError, match='unplanned'):
        reset.reset(system.s3, system.db, system.sqs, system.ecs, plan, sleep=lambda _: None)
    assert not system.s3.deleted and not system.db.deleted and not system.sqs.purged


def test_s3_delete_errors_stop_before_database_or_queue_changes(system):
    plan = reset.inventory(system.s3, system.db)
    system.s3.errors = True
    with pytest.raises(RuntimeError, match='S3 version deletion'):
        reset.reset(system.s3, system.db, system.sqs, system.ecs, plan, sleep=lambda _: None)
    assert not system.db.deleted and not system.sqs.purged


def test_s3_partial_transient_error_retries_only_failed_version(system):
    plan = reset.inventory(system.s3, system.db)
    original_delete = system.s3.delete_objects
    attempts = []
    def partial_delete(**kwargs):
        objects = kwargs['Delete']['Objects']
        attempts.append(deepcopy(objects))
        if len(attempts) == 1:
            original_delete(**{**kwargs, 'Delete': {'Objects': objects[:1]}})
            return {'Errors': [{'Code': 'InternalError', **objects[1]}]}
        return original_delete(**kwargs)
    system.s3.delete_objects = partial_delete
    sleeps = []
    reset.reset(system.s3, system.db, system.sqs, system.ecs, plan, sleep=sleeps.append)
    assert attempts == [plan['versions'], plan['versions'][1:]]
    assert system.s3.deleted == plan['versions'] and system.sqs.purged
    assert sleeps == [1, 1, 60]


def test_s3_transient_error_retry_budget_is_bounded(system):
    plan = reset.inventory(system.s3, system.db)
    attempts = []
    def unavailable(**kwargs):
        attempts.append(kwargs)
        return {'Errors': [{'Code': 'ServiceUnavailable', **v} for v in kwargs['Delete']['Objects']]}
    system.s3.delete_objects = unavailable
    with pytest.raises(RuntimeError, match='ServiceUnavailable'):
        reset.reset(system.s3, system.db, system.sqs, system.ecs, plan, sleep=lambda _: None)
    assert len(attempts) == 5 and not system.db.deleted and not system.sqs.purged


def test_partial_reset_can_resume_from_original_inventory(system):
    plan = reset.inventory(system.s3, system.db)
    system.s3.versions.pop()
    system.db.rows['finbot-prod-filings'].clear()
    reset.reset(system.s3, system.db, system.sqs, system.ecs, plan, sleep=lambda _: None)
    assert system.sqs.purged


def test_cli_inventory_closes_sdk_clients_and_never_writes_cloud(system, monkeypatch, tmp_path):
    import json
    closed = []
    clients = {'s3': system.s3, 'dynamodb': system.db, 'ecs': system.ecs, 'sqs': system.sqs,
               'sts': SimpleNamespace(get_caller_identity=lambda: {'Account': reset.ACCOUNT})}
    for name, client in clients.items():
        client.close = lambda name=name: closed.append(name)
    session = SimpleNamespace(client=lambda name, **kwargs: clients[name])
    monkeypatch.setattr(reset.boto3, 'Session', lambda **kwargs: session)
    path = tmp_path / 'inventory.json'
    monkeypatch.setattr('sys.argv', ['reset', '--manifest', str(path)])
    assert reset.main() == 0
    plan = json.loads(path.read_text())
    assert plan['task_definition'] == 'new' and len(plan['versions']) == 2
    assert set(closed) == set(clients)
    assert not system.s3.deleted and not system.db.deleted and not system.sqs.purged
