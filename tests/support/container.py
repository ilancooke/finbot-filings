"""Reusable offline container test support."""

from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
import json
import os
import subprocess
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tests/container"
IMAGE = os.environ.get("FINBOT_CONTAINER_IMAGE", "finbot-ingestion:local")
ENV = {
    "SEC_USER_AGENT": "Finbot fixture fixture@example.invalid", "AWS_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_EC2_METADATA_DISABLED": "true", "COMPANIES_TABLE": "companies",
    "CALENDAR_TABLE": "calendar", "FILINGS_TABLE": "filings", "ARTIFACTS_TABLE": "artifacts",
    "ARTIFACT_BUCKET": "fixture-artifacts",
    "FILING_READY_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:fixture",
    "INGESTION_DEAD_LETTER_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123456789012/fixture",
    "RUNTIME_SAFETY_POLL_SECONDS": ".3", "RUNTIME_ACTIVE_POLL_SECONDS": ".1",
    "RUNTIME_HEARTBEAT_SECONDS": ".1", "RUNTIME_TICK_SECONDS": ".05",
    "RUNTIME_SHUTDOWN_GRACE_SECONDS": ".1", "RUNTIME_METRICS_FLUSH_SECONDS": ".2",
}


def run(*args, check=True, env=None):
    result = subprocess.run(args, text=True, capture_output=True, timeout=45, env=env)
    if check and result.returncode:
        pytest.fail(f"command failed ({result.returncode}): {result.stderr[-3000:]}")
    return result


def command_environment(tmp_path):
    # An allowlist prevents credentials/configuration leaking into child processes.
    return {"PATH": os.environ["PATH"], "PYTHONPATH": str(HARNESS),
        "AWS_EC2_METADATA_DISABLED": "true", "RUNTIME_HEALTH_PATH": str(tmp_path / "health.json"),
        "PYTHONDONTWRITEBYTECODE": "1"}


@pytest.fixture
def docker():
    if os.environ.get("FINBOT_CONTAINER_TESTS") != "1":
        pytest.skip("Docker lifecycle tests require FINBOT_CONTAINER_TESTS=1 and a rebuilt image")
    run("docker", "image", "inspect", IMAGE)
    return lambda *args, **kwargs: run("docker", *args, **kwargs)


@contextmanager
def fixture_container(docker, scenario="normal"):
    name = "finbot-lifecycle-" + uuid4().hex[:12]
    options = ["run", "-d", "--name", name, "--network", "none", "--read-only",
               "--tmpfs", "/tmp:rw,nosuid,size=32m", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--stop-timeout", "30",
               "--health-interval", "1s", "--health-start-period", "5s",
               "--mount", f"type=bind,src={HARNESS},dst=/harness,readonly",
               "--mount", f"type=bind,src={ROOT / 'tests'},dst=/test-support/tests,readonly",
               "--mount", f"type=bind,src={ROOT / 'tests/fixtures/sec_package'},dst=/fixtures,readonly"]
    yahoo = ({"CALENDAR_PROVIDER":"yahoo", "CALENDAR_PROVIDER_ATTEMPTS":"1",
        "YAHOO_MIN_REQUEST_INTERVAL_SECONDS":".005", "YAHOO_MAX_FETCH_SECONDS":"60"} if scenario.startswith("yahoo") else {})
    for key, value in {**ENV, **yahoo, "PYTHONPATH": "/harness:/test-support", "FINBOT_TEST_SCENARIO": scenario}.items():
        options.extend(["-e", key + "=" + value])
    try:
        docker(*options, IMAGE)
        yield name
    finally:
        docker("rm", "-f", name, check=False)


def read_json(docker, name, path):
    result = docker("exec", name, "cat", path, check=False)
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def trace(docker, name):
    result = docker("exec", name, "cat", "/tmp/fixture/trace.jsonl", check=False)
    return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]


def final_result(docker, name):
    logs = docker("logs", name).stdout
    return json.loads(next(line.removeprefix("FINBOT_FIXTURE_RESULT=")
                          for line in logs.splitlines() if line.startswith("FINBOT_FIXTURE_RESULT=")))


def until(predicate, docker, name, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.1)
    pytest.fail("container condition timed out: " + docker("logs", name).stdout[-3000:])
