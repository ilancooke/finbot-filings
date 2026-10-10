"""Finite operator-invoked Fargate diagnostic, independent of normal ingestion."""

import argparse
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import resource
import signal
import sys
from tempfile import TemporaryDirectory
import time

from botocore.config import Config
from botocore.exceptions import ClientError

from .aws_config import StorageConfig
from .ingestion.config import WorkflowConfig
from .observability.health import HealthFile
from .observability.metrics import Metrics
from .repositories.dynamodb.config import DynamoDBConfig
from .repositories.dynamodb.companies import DynamoDBCompanyRepository
from .repositories.dynamodb.serialization import decode, encode

MAX_SECONDS = 180


class DeadlineExceeded(BaseException):
    """Escape SDK Exception handlers that might otherwise wrap/retry the alarm."""


def fixture_keys(check_id):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", check_id):
        raise ValueError("invalid check identifier")
    reserved = "__deployment_check__:" + check_id
    return {
        "calendar": {"expected_date": "__deployment_check__", "cik": reserved},
        "filings": {"accession_number": reserved},
        "artifacts": {"artifact_id": reserved},
    }


def expect_s3_error(operation, status):
    # HEAD uses generic numeric error codes; check HTTP status, not exception text.
    try:
        operation()
    except ClientError as exc:
        if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == status:
            return
        raise
    raise RuntimeError("S3 operation unexpectedly succeeded")


def check_companies(db, table, expected=50):
    ciks, token = set(), None
    for _ in range(10):
        request = dict(TableName=table, IndexName="EnabledCompanies", Limit=100,
            KeyConditionExpression="enabled_marker = :enabled",
            ExpressionAttributeValues=encode({":enabled": "ENABLED"}))
        if token:
            request["ExclusiveStartKey"] = token
        page = db.query(**request)
        for wire in page["Items"]:
            cik = decode(wire)["cik"]
            if cik in ciks or len(ciks) >= expected:
                raise RuntimeError("enabled universe differs from reviewed scope")
            ciks.add(cik)
        token = page.get("LastEvaluatedKey")
        if not token:
            break
    if token or len(ciks) != expected:
        raise RuntimeError("enabled universe count or pagination differs from reviewed scope")
    for cik in sorted(ciks):
        row = db.get_item(TableName=table, Key=encode({"cik": cik}), ConsistentRead=True)
        company = DynamoDBCompanyRepository._validate(decode(row["Item"]))
        if not company.enabled or company.cik != cik:
            raise RuntimeError("enabled index disagrees with base company")
    return len(ciks)


def check_checkpoints(db, config, check_id, report):
    for name, key in fixture_keys(check_id).items():
        table = getattr(config, name + "_table")
        report["fixtures"][name] = {"table": table, "key": key}
        # No application schema or sparse-index attributes: workers cannot discover
        # these rows through recovery, accession children or date-range queries.
        item = {**key, "deployment_check_id": check_id, "deployment_check_state": "created"}
        db.put_item(TableName=table, Item=encode(item),
            ConditionExpression="attribute_not_exists(#pk)",
            ExpressionAttributeNames={"#pk": next(iter(key))})
        db.update_item(TableName=table, Key=encode(key),
            UpdateExpression="SET deployment_check_state = :updated",
            ConditionExpression="deployment_check_id = :id AND deployment_check_state = :created",
            ExpressionAttributeValues=encode({":id": check_id, ":created": "created", ":updated": "verified"}))
        read = decode(db.get_item(TableName=table, Key=encode(key), ConsistentRead=True)["Item"])
        if read != {**item, "deployment_check_state": "verified"}:
            raise RuntimeError("checkpoint readback differs")
    report["checkpoint_roundtrips"] = 3


def check_s3(s3, bucket, check_id, report):
    key = "deployment-checks/" + check_id + "/probe.txt"
    report["fixtures"]["s3"] = fixture = {"bucket": bucket, "key": key}
    expect_s3_error(lambda: s3.head_object(Bucket=bucket, Key=key), 404)
    body = ("finbot deployment check " + check_id + "\n").encode()
    created = s3.put_object(Bucket=bucket, Key=key, Body=body,
        ContentType="text/plain", IfNoneMatch="*", ServerSideEncryption="AES256")
    fixture["version_id"] = created.get("VersionId")
    expect_s3_error(lambda: s3.put_object(Bucket=bucket, Key=key, Body=b"overwrite", IfNoneMatch="*"), 412)
    expect_s3_error(lambda: s3.put_object(Bucket=bucket, Key=key, Body=b"unconditional overwrite"), 403)
    head = s3.head_object(Bucket=bucket, Key=key)
    if head["ContentLength"] != len(body) or head.get("ServerSideEncryption") != "AES256":
        raise RuntimeError("S3 size or service-managed encryption differs")
    if not fixture["version_id"] or head.get("VersionId") != fixture["version_id"]:
        raise RuntimeError("S3 version changed or versioning is absent")
    with s3.get_object(Bucket=bucket, Key=key)["Body"] as stream:
        if stream.read(len(body) + 1) != body:
            raise RuntimeError("S3 original bytes changed")
    report["s3_absence_and_immutability"] = True


def check_local(environ, report):
    if sys.platform != "linux" or os.getuid() != 10001 or not os.statvfs("/").f_flag & os.ST_RDONLY:
        raise RuntimeError("check requires the production Linux user and read-only root")
    with TemporaryDirectory(prefix="finbot-deployment-check-", dir="/tmp") as directory:
        health = HealthFile(Path(directory) / "heartbeat.json")
        health.write({"live": True, "heartbeat_at": datetime.now(timezone.utc).isoformat()})
        if not HealthFile.check(health.path):
            raise RuntimeError("local heartbeat roundtrip failed")
        health.remove()
        if health.path.exists():
            raise RuntimeError("local heartbeat cleanup failed")
        cache = Path(directory) / "yahoo-cache"
        cache.mkdir(mode=0o700)
        probe = cache / "probe"
        probe.write_bytes(b"private writable cache")
        if probe.read_bytes() != b"private writable cache" or cache.stat().st_mode & 0o777 != 0o700:
            raise RuntimeError("private cache roundtrip failed")
    report["writable_tmp_and_heartbeat"] = True
    # Load the major native dependencies before touching the configured two-copy
    # buffers. This is a synthetic capacity check, not observed ingestion peak.
    import yfinance  # noqa: F401
    from .scheduler.market_sessions import ExchangeMarketSessions
    today = datetime.now(timezone.utc).date()
    ExchangeMarketSessions(start=today.replace(month=1, day=1),
        end=today.replace(month=12, day=31)).session(today)
    storage = StorageConfig.from_env(environ)
    workflow = WorkflowConfig.from_env(environ)
    size = 2 * workflow.max_inflight_artifacts * storage.max_artifact_bytes
    if size > 256 * 1024 * 1024:
        raise RuntimeError("diagnostic memory allocation exceeds its 256 MiB bound")
    buffers = [bytearray(storage.max_artifact_bytes) for _ in range(2 * workflow.max_inflight_artifacts)]
    for buffer in buffers:
        for offset in range(0, len(buffer), 4096):
            buffer[offset] = 1
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    report.update(synthetic_buffer_mib=size // (1024 * 1024), synthetic_peak_rss_mib=round(peak, 1))
    if peak >= 768:
        raise RuntimeError("synthetic peak leaves less than 256 MiB of the initial 1 GiB task")


def run(check_id, report, environ=None):
    import boto3
    env = os.environ if environ is None else environ
    config = DynamoDBConfig.from_env(env)
    storage = StorageConfig.from_env(env)
    if env.get("AWS_EXECUTION_ENV") != "AWS_ECS_FARGATE":
        raise RuntimeError("check requires Fargate task credentials")
    session = boto3.Session(region_name=config.region)
    credentials = session.get_credentials()
    if credentials is None or credentials.method != "container-role":
        raise RuntimeError("check refuses credentials outside the ECS task role")
    report["credential_source"] = "container-role"
    check_local(env, report)
    sdk = Config(connect_timeout=3, read_timeout=5, retries={"mode": "standard", "total_max_attempts": 2},
        ignore_configured_endpoint_urls=True)
    with ExitStack() as stack:
        db = stack.enter_context(closing(session.client("dynamodb", config=sdk)))
        s3 = stack.enter_context(closing(session.client("s3", config=sdk)))
        report["enabled_companies"] = check_companies(db, config.companies_table)
        check_checkpoints(db, config, check_id, report)
        check_s3(s3, storage.bucket, check_id, report)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Finite Fargate check; creates isolated AWS test fixtures")
    parser.add_argument("--check-id", required=True)
    args = parser.parse_args(argv)
    try:
        fixture_keys(args.check_id)
    except ValueError:
        parser.error("--check-id must be 1-48 lowercase letters, digits or hyphens")
    report = {"event": "deployment_check", "check_id": args.check_id, "fixtures": {}, "max_seconds": MAX_SECONDS}
    started = time.monotonic()
    def expired(signum, frame):
        raise DeadlineExceeded("deployment check deadline exceeded")
    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(MAX_SECONDS)
    code = 1
    try:
        run(args.check_id, report)
        report["status"] = "passed"
        code = 0
    except (Exception, KeyboardInterrupt, DeadlineExceeded) as exc:
        # SDK/provider exceptions can contain credentials or authenticated URLs.
        report.update(status="failed", error_type=type(exc).__name__)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    report["duration_seconds"] = round(time.monotonic() - started, 3)
    print(json.dumps(report, sort_keys=True))
    metrics = Metrics(namespace="Finbot/DeploymentChecks", environment=os.environ.get("RUNTIME_METRICS_ENVIRONMENT", "local"))
    metrics.count("DeploymentCheckSucceeded", int(code == 0))
    metrics.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
