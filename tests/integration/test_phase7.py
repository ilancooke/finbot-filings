"""Installed command behavior and opt-in Docker lifecycle, always offline."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tests/container"
IMAGE = os.environ.get("FINBOT_CONTAINER_IMAGE", "finbot-ingestion:phase7")
ENV = {
    "SEC_USER_AGENT": "Finbot fixture fixture@example.invalid", "AWS_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_EC2_METADATA_DISABLED": "true", "COMPANIES_TABLE": "companies",
    "CALENDAR_TABLE": "calendar", "FILINGS_TABLE": "filings", "ARTIFACTS_TABLE": "artifacts",
    "ARTIFACT_BUCKET": "fixture-artifacts",
    "ARTIFACT_READY_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:fixture",
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


def test_module_help_without_service_configuration(tmp_path):
    result = run(sys.executable, "-m", "finbot_ingestion.main", "--help", env=command_environment(tmp_path))
    assert "--health-check" in result.stdout


@pytest.mark.parametrize("state", ["missing", "malformed", "naive", "stale", "future", "unhealthy", "live"])
def test_health_command_in_guarded_subprocess_without_aws(tmp_path, state):
    env = command_environment(tmp_path)
    path = Path(env["RUNTIME_HEALTH_PATH"])
    now = datetime.now(timezone.utc)
    if state == "malformed":
        path.write_text("invalid")
    elif state != "missing":
        at = now - timedelta(seconds=1000) if state == "stale" else now + timedelta(seconds=1000) if state == "future" else now
        if state == "naive":
            at = at.replace(tzinfo=None)
        path.write_text(json.dumps({"live": state != "unhealthy", "heartbeat_at": at.isoformat()}))
    result = run(sys.executable, "-m", "finbot_ingestion.main", "--health-check", check=False, env=env)
    assert result.returncode == (0 if state == "live" else 1)
    assert not result.stderr


def test_invalid_startup_is_sanitized_and_offline(tmp_path):
    env = command_environment(tmp_path)
    env["SEC_USER_AGENT"] = ""
    result = run(sys.executable, "-m", "finbot_ingestion.main", check=False, env=env)
    assert result.returncode == 1
    assert "ConfigurationError" in result.stderr and "Traceback" not in result.stderr


@pytest.fixture
def docker():
    if os.environ.get("FINBOT_CONTAINER_TESTS") != "1":
        pytest.skip("Docker lifecycle tests require FINBOT_CONTAINER_TESTS=1 and a rebuilt image")
    run("docker", "image", "inspect", IMAGE)
    return lambda *args, **kwargs: run("docker", *args, **kwargs)


@contextmanager
def fixture_container(docker, scenario="normal"):
    name = "finbot-phase7-" + uuid4().hex[:12]
    options = ["run", "-d", "--name", name, "--network", "none", "--read-only",
               "--tmpfs", "/tmp:rw,nosuid,size=32m", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--stop-timeout", "30",
               "--health-interval", "1s", "--health-start-period", "5s",
               "--mount", f"type=bind,src={HARNESS},dst=/harness,readonly",
               "--mount", f"type=bind,src={ROOT / 'tests/integration'},dst=/support,readonly",
               "--mount", f"type=bind,src={ROOT / 'tests/fixtures/sec_package'},dst=/fixtures,readonly"]
    for key, value in {**ENV, "PYTHONPATH": "/harness:/support", "FINBOT_TEST_SCENARIO": scenario}.items():
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


@pytest.mark.container
def test_production_image_installation_and_entrypoint(docker):
    config = json.loads(docker("image", "inspect", IMAGE).stdout)[0]["Config"]
    assert config["Entrypoint"] == ["python", "-m", "finbot_ingestion.main"]
    assert config["User"] == "10001:10001"
    assert config["Healthcheck"]["Test"] == ["CMD", "python", "-m", "finbot_ingestion.main", "--health-check"]
    assert "--health-check" in docker("run", "--rm", "--network", "none", IMAGE, "--help").stdout
    assert docker("run", "--rm", "--network", "none", IMAGE, "--health-check", check=False).returncode == 1
    failure = docker("run", "--rm", "--network", "none", IMAGE, check=False)
    assert failure.returncode == 1 and "ConfigurationError" in failure.stderr
    code = '''import importlib.util, os, pkgutil
from datetime import date
from importlib.metadata import distribution
import finbot_ingestion
from finbot_ingestion.scheduler.market_sessions import ExchangeMarketSessions
assert os.getuid() == 10001
for name in ("finbot_filings", "pyarrow", "lxml", "pytest", "runtime_fixture"):
    assert importlib.util.find_spec(name) is None, name
assert not distribution("finbot-filings").entry_points
for item in pkgutil.walk_packages(finbot_ingestion.__path__, "finbot_ingestion."):
    __import__(item.name)
assert ExchangeMarketSessions(start=date(2026,1,1), end=date(2026,12,31)).session(date(2026,10,8))
print("installed runtime and XNYS session verified")'''
    docker("run", "--rm", "--network", "none", "--entrypoint", "python", IMAGE, "-c", code)
    assert "No broken requirements" in docker("run", "--rm", "--network", "none", "--entrypoint", "python", IMAGE,
                                            "-m", "pip", "check").stdout


@pytest.mark.container
@pytest.mark.parametrize("sig", ["SIGTERM", "SIGINT"])
def test_real_signals_and_artifact_path(docker, sig):
    with fixture_container(docker) as name:
        health = until(lambda: read_json(docker, name, "/tmp/finbot-ingestion-health.json"), docker, name)
        assert health["live"] and health["pid"] == 1
        assert not health["provider_configured"] and health["calendar_stale"]
        assert len(health["tasks"]) == 11
        until(lambda: sum(t.get("operation") == "Publish" for t in trace(docker, name)) == 4, docker, name)
        assert docker("exec", name, "python", "-m", "finbot_ingestion.main", "--health-check").returncode == 0
        until(lambda: json.loads(docker("inspect", name).stdout)[0]["State"]["Health"]["Status"] == "healthy", docker, name)
        docker("kill", "--signal", sig, name)
        assert docker("wait", name).stdout.strip() == "0", docker("logs", name).stderr
        result = final_result(docker, name)
        assert len(result["tables"]) == len(result["objects"]) == len(result["events"]) == 4
        assert set(result["objects"]) == {b"\x00SEC\xff\r\noriginal".hex()}
        assert all(row["published_at"] and row["s3_uri"] for row in result["tables"])
        assert all(event["schema_version"] == "1.0" for event in result["events"])
        assert not result["health_exists"]
        assert any(t["kind"] == "shutdown" and t["tasks_done"] for t in result["trace"])
        assert any(t["kind"] == "stop" and not t["accepting"] for t in result["trace"])
        assert {t["service"] for t in result["trace"] if t["kind"] == "client_closed"} == {"dynamodb", "s3", "sns", "sqs"}
        assert "Ingestion runtime failed" not in docker("logs", name).stderr


@pytest.mark.container
def test_blocking_shutdown_awaits_io_and_closes_clients(docker):
    with fixture_container(docker, "blocking") as name:
        until(lambda: any(t["kind"] == "blocked" for t in trace(docker, name)), docker, name)
        docker("kill", "--signal", "SIGTERM", name)
        until(lambda: any(t["kind"] == "stop" and t["accepting"] is False for t in trace(docker, name)), docker, name)
        time.sleep(.3)  # Beyond the .1-second drain grace; blocking I/O still owns admission.
        assert json.loads(docker("inspect", name).stdout)[0]["State"]["Running"]
        assert not any(t["kind"] == "client_closed" for t in trace(docker, name))
        docker("exec", name, "touch", "/tmp/fixture/release")
        assert docker("wait", name).stdout.strip() == "0", docker("logs", name).stderr
        result = final_result(docker, name)
        kinds = [t["kind"] for t in result["trace"]]
        assert kinds.index("released") < kinds.index("sec_closed") < kinds.index("client_closed")
        assert not result["health_exists"]
        assert any(t["kind"] == "shutdown" and t["tasks_done"] for t in result["trace"])


@pytest.mark.container
@pytest.mark.parametrize("scenario", ["failure", "return"])
def test_required_loop_exit_fails_process(docker, scenario):
    with fixture_container(docker, scenario) as name:
        assert docker("wait", name).stdout.strip() == "1"
        assert "Required runtime task stopped" in docker("logs", name).stderr
        result = final_result(docker, name)
        assert not result["health_exists"]
        assert any(t["kind"] == "shutdown" and t["tasks_done"] for t in result["trace"])
