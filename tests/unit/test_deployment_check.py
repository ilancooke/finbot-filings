"""Offline SDK contracts and isolation for the operator's finite cloud check."""

from contextlib import closing
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace

import boto3
from botocore.response import StreamingBody
from botocore.stub import Stubber
from botocore.validate import validate_parameters
import pytest

from finbot_ingestion import deployment_check as check
from finbot_ingestion.main import main
from finbot_ingestion.repositories.dynamodb.config import DynamoDBConfig
from finbot_ingestion.repositories.dynamodb.serialization import encode

CONFIG = DynamoDBConfig("us-east-1", "companies", "calendar", "filings", "artifacts")
ENV = {"AWS_REGION": "us-east-1", "COMPANIES_TABLE": "companies", "CALENDAR_TABLE": "calendar",
       "FILINGS_TABLE": "filings", "ARTIFACTS_TABLE": "artifacts", "ARTIFACT_BUCKET": "finbot-probe-bucket"}


def test_main_dispatch_cannot_start_application(monkeypatch):
    calls = []
    monkeypatch.setattr(check, "main", lambda argv: calls.append(argv) or 7)
    def forbidden(*args):
        pytest.fail("diagnostic must not construct normal ingestion")
    assert main(["--deployment-check", "--check-id", "test-1"], builder=forbidden) == 7
    assert calls == [["--check-id", "test-1"]]
    for argv in (["--deployment-check"], ["--check-id", "test-1"],
                 ["--deployment-check", "--health-check"]):
        with pytest.raises(SystemExit):
            main(argv, builder=forbidden)


@pytest.mark.parametrize("identifier", ["", "../bad", "UPPER", "a" * 49])
def test_fixture_identifiers_are_bounded(identifier):
    with pytest.raises(ValueError):
        check.fixture_keys(identifier)


def test_gsi_keys_are_strongly_rechecked_and_pagination_is_not_ignored():
    with closing(boto3.client("dynamodb", region_name="us-east-1")) as db, Stubber(db) as stub:
        query = dict(TableName="companies", IndexName="EnabledCompanies", Limit=100,
            KeyConditionExpression="enabled_marker = :enabled",
            ExpressionAttributeValues=encode({":enabled": "ENABLED"}))
        key = encode({"cik": "0000000001"})
        # An empty page with a token is not terminal.
        stub.add_response("query", {"Items": [], "LastEvaluatedKey": key}, query)
        stub.add_response("query", {"Items": [key]}, {**query, "ExclusiveStartKey": key})
        stub.add_response("get_item", {"Item": encode({"repository_schema_version": 1,
            "cik": "0000000001", "ticker": "TEST", "name": "Test company",
            "enabled": True, "enabled_marker": "ENABLED"})},
            {"TableName": "companies", "Key": key, "ConsistentRead": True})
        assert check.check_companies(db, "companies", expected=1) == 1
        stub.assert_no_pending_responses()


def test_company_scope_mismatch_fails_before_writes():
    with closing(boto3.client("dynamodb", region_name="us-east-1")) as db, Stubber(db) as stub:
        stub.add_response("query", {"Items": []})
        with pytest.raises(RuntimeError):
            check.check_companies(db, "companies")
        stub.assert_no_pending_responses()


def test_checkpoint_fixtures_are_create_only_and_absent_from_sparse_indexes():
    report = {"fixtures": {}}
    with closing(boto3.client("dynamodb", region_name="us-east-1")) as db, Stubber(db) as stub:
        for table, key in check.fixture_keys("test-1").items():
            item = {**key, "deployment_check_id": "test-1", "deployment_check_state": "created"}
            assert not set(item) & {"pending_work_kind", "pending_work_sort", "filename", "enabled_marker"}
            if table == "artifacts":
                assert "accession_number" not in item
            stub.add_response("put_item", {}, dict(TableName=table, Item=encode(item),
                ConditionExpression="attribute_not_exists(#pk)", ExpressionAttributeNames={"#pk": next(iter(key))}))
            stub.add_response("update_item", {}, dict(TableName=table, Key=encode(key),
                UpdateExpression="SET deployment_check_state = :updated",
                ConditionExpression="deployment_check_id = :id AND deployment_check_state = :created",
                ExpressionAttributeValues=encode({":id": "test-1", ":created": "created", ":updated": "verified"})))
            stub.add_response("get_item", {"Item": encode({**item, "deployment_check_state": "verified"})},
                dict(TableName=table, Key=encode(key), ConsistentRead=True))
        check.check_checkpoints(db, CONFIG, "test-1", report)
        assert report["checkpoint_roundtrips"] == 3
        stub.assert_no_pending_responses()


def test_s3_absence_immutability_and_body_closed():
    report = {"fixtures": {}}
    body = b"finbot deployment check test-1\n"
    raw = BytesIO(body)
    request = {"Bucket": "finbot-probe-bucket", "Key": "deployment-checks/test-1/probe.txt"}
    with closing(boto3.client("s3", region_name="us-east-1")) as s3, Stubber(s3) as stub:
        stub.add_client_error("head_object", "404", http_status_code=404, expected_params=request)
        stub.add_response("put_object", {"VersionId": "v1"}, {**request, "Body": body,
            "ContentType": "text/plain", "IfNoneMatch": "*", "ServerSideEncryption": "AES256"})
        stub.add_client_error("put_object", "PreconditionFailed", http_status_code=412,
            expected_params={**request, "Body": b"overwrite", "IfNoneMatch": "*"})
        stub.add_client_error("put_object", "AccessDenied", http_status_code=403,
            expected_params={**request, "Body": b"unconditional overwrite"})
        stub.add_response("head_object", {"ContentLength": len(body), "ServerSideEncryption": "AES256", "VersionId": "v1"}, request)
        stub.add_response("get_object", {"Body": StreamingBody(raw, len(body))}, request)
        check.check_s3(s3, request["Bucket"], "test-1", report)
        assert report["s3_absence_and_immutability"] and raw.closed
        stub.assert_no_pending_responses()


def test_head_access_denied_is_not_absence():
    with closing(boto3.client("s3", region_name="us-east-1")) as s3, Stubber(s3) as stub:
        stub.add_client_error("head_object", "AccessDenied", http_status_code=403)
        with pytest.raises(s3.exceptions.ClientError):
            check.check_s3(s3, "finbot-probe-bucket", "test-1", {"fixtures": {}})
        stub.assert_no_pending_responses()


def test_refuses_local_and_static_credentials(monkeypatch):
    with pytest.raises(RuntimeError, match="Fargate"):
        check.run("test-1", {"fixtures": {}}, ENV)
    monkeypatch.setattr(boto3, "Session", lambda **kw: SimpleNamespace(
        get_credentials=lambda: SimpleNamespace(method="env")))
    with pytest.raises(RuntimeError, match="ECS task role"):
        check.run("test-1", {"fixtures": {}}, {**ENV, "AWS_EXECUTION_ENV": "AWS_ECS_FARGATE"})


def test_local_heartbeat_cache_and_touched_buffer_budget(monkeypatch):
    from finbot_ingestion.scheduler import market_sessions
    monkeypatch.setattr(check.sys, "platform", "linux")
    monkeypatch.setattr(check.os, "getuid", lambda: 10001)
    monkeypatch.setattr(check.os, "statvfs", lambda path: SimpleNamespace(f_flag=check.os.ST_RDONLY))
    monkeypatch.setitem(check.sys.modules, "yfinance", SimpleNamespace())
    monkeypatch.setattr(market_sessions, "ExchangeMarketSessions",
        lambda **kw: SimpleNamespace(session=lambda day: None))
    monkeypatch.setattr(check.resource, "getrusage", lambda who: SimpleNamespace(ru_maxrss=96 * 1024))
    report = {}
    check.check_local({**ENV, "MAX_ARTIFACT_BYTES": str(4 * 1024 * 1024)}, report)
    assert report == {"writable_tmp_and_heartbeat": True,
        "synthetic_buffer_mib": 16, "synthetic_peak_rss_mib": 96.0}
    with pytest.raises(RuntimeError, match="256 MiB bound"):
        check.check_local({**ENV, "INGESTION_MAX_INFLIGHT_ARTIFACTS": "3"}, {})


def test_duplicate_checkpoint_stops_without_updating_existing_record():
    report = {"fixtures": {}}
    with closing(boto3.client("dynamodb", region_name="us-east-1")) as db, Stubber(db) as stub:
        stub.add_client_error("put_item", "ConditionalCheckFailedException")
        with pytest.raises(db.exceptions.ConditionalCheckFailedException):
            check.check_checkpoints(db, CONFIG, "test-1", report)
        assert list(report["fixtures"]) == ["calendar"]
        stub.assert_no_pending_responses()


def test_deadline_interrupts_check_and_restores_handler(monkeypatch, capsys):
    handlers = []
    previous = object()
    monkeypatch.setattr(check.signal, "signal", lambda sig, handler: handlers.append(handler) or previous)
    monkeypatch.setattr(check.signal, "alarm", lambda seconds: None)
    monkeypatch.setattr(check, "run", lambda identifier, report: handlers[0](None, None))
    assert check.main(["--check-id", "test-1"]) == 1
    assert json.loads(capsys.readouterr().out.splitlines()[0])["error_type"] == "DeadlineExceeded"
    assert handlers[1] is previous


@pytest.mark.parametrize("exception", [RuntimeError("secret URL or credential"), TimeoutError("timeout")])
def test_failure_sanitized_and_deadline_disarmed(monkeypatch, capsys, exception):
    def fail(identifier, report):
        report["fixtures"]["partial"] = {"key": "known-test-key"}
        raise exception
    monkeypatch.setattr(check, "run", fail)
    alarms = []
    monkeypatch.setattr(check.signal, "alarm", alarms.append)
    assert check.main(["--check-id", "test-1"]) == 1
    output = capsys.readouterr().out
    report, metric = map(json.loads, output.splitlines())
    assert "secret" not in output and report["status"] == "failed"
    assert report["fixtures"]["partial"]["key"] == "known-test-key"
    assert alarms == [180, 0]
    assert metric["DeploymentCheckSucceeded"] == 0 and "RuntimeHealthy" not in metric
    assert metric["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "Finbot/DeploymentChecks"


def test_run_task_input_is_valid_and_changes_only_command():
    path = Path(__file__).resolve().parents[2] / "infra/checks/run-task.prod.json"
    request = json.loads(path.read_text())
    with closing(boto3.client("ecs", region_name="us-east-1")) as ecs:
        validate_parameters({**request, "taskDefinition": "finbot-prod-ingestion:4"},
            ecs.meta.service_model.operation_model("RunTask").input_shape)
    assert request["count"] == 1
    override, = request["overrides"]["containerOverrides"]
    assert override == {"name": "ingestion", "command": ["--deployment-check", "--check-id", "readiness-20261009-1"]}
    assert "taskDefinition" not in request  # Operator must supply the reviewed staged revision.


def test_cleanup_input_targets_only_owned_diagnostic_rows():
    path = Path(__file__).resolve().parents[2] / "infra/checks/delete-checkpoints.prod.json"
    request = json.loads(path.read_text())
    with closing(boto3.client("dynamodb", region_name="us-east-1")) as db:
        validate_parameters(request, db.meta.service_model.operation_model("TransactWriteItems").input_shape)
    keys = check.fixture_keys("readiness-20261009-1")
    assert len(request["TransactItems"]) == 3
    for action, (name, key) in zip(request["TransactItems"], keys.items(), strict=True):
        assert set(action) == {"Delete"}
        assert action["Delete"] == {
            "TableName": "arn:aws:dynamodb:us-east-1:559007813222:table/finbot-prod-" + name,
            "Key": encode(key), "ConditionExpression": "deployment_check_id = :id",
            "ExpressionAttributeValues": encode({":id": "readiness-20261009-1"})}
