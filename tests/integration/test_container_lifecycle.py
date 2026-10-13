"""Opt-in offline production-image and Docker lifecycle tests."""

import json
import time

import pytest

from tests.support.container import (
    IMAGE,
    ENV,
    fixture_container,
    read_json,
    trace,
    final_result,
    until,
)


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
for name in ("finbot_filings", "pyarrow", "pytest", "runtime_fixture"):
    assert importlib.util.find_spec(name) is None, name
assert not distribution("finbot-filings").entry_points
assert distribution("yfinance").version == "1.7.0"
for name in ("lxml", "curl_cffi", "yfinance"):
    assert importlib.util.find_spec(name) is not None, name
for item in pkgutil.walk_packages(finbot_ingestion.__path__, "finbot_ingestion."):
    __import__(item.name)
assert ExchangeMarketSessions(start=date(2026,1,1), end=date(2026,12,31)).session(date(2026,10,8))
print("installed runtime and XNYS session verified")'''
    docker("run", "--rm", "--network", "none", "--entrypoint", "python", IMAGE, "-c", code)
    assert "No broken requirements" in docker("run", "--rm", "--network", "none", "--entrypoint", "python", IMAGE,
                                            "-m", "pip", "check").stdout
    # Exercise the installed diagnostic through the real image ENTRYPOINT. Even
    # with Fargate's marker, static credentials must fail before AWS or ingestion.
    options = ["run", "--rm", "--network", "none"]
    for key, value in {**ENV, "AWS_EXECUTION_ENV": "AWS_ECS_FARGATE"}.items():
        options.extend(["-e", key + "=" + value])
    probe = docker(*options, IMAGE, "--deployment-check", "--check-id", "container-check", check=False)
    report, metric = map(json.loads, probe.stdout.splitlines())
    assert probe.returncode == 1 and report["status"] == "failed"
    assert report["error_type"] == "RuntimeError" and report["fixtures"] == {}
    assert metric["DeploymentCheckSucceeded"] == 0
    assert "Ingestion runtime failed" not in probe.stderr


@pytest.mark.container
@pytest.mark.parametrize("sig", ["SIGTERM", "SIGINT"])
def test_real_signals_and_artifact_path(docker, sig):
    with fixture_container(docker) as name:
        health = until(lambda: read_json(docker, name, "/tmp/finbot-ingestion-health.json"), docker, name)
        assert health["live"] and health["pid"] == 1
        assert not health["provider_configured"] and health["calendar_stale"]
        assert len(health["tasks"]) == 11
        until(lambda: sum(t.get("operation") == "Publish" for t in trace(docker, name)) == 2, docker, name)
        assert docker("exec", name, "python", "-m", "finbot_ingestion.main", "--health-check").returncode == 0
        until(lambda: json.loads(docker("inspect", name).stdout)[0]["State"]["Health"]["Status"] == "healthy", docker, name)
        docker("kill", "--signal", sig, name)
        assert docker("wait", name).stdout.strip() == "0", docker("logs", name).stderr
        result = final_result(docker, name)
        assert len(result["tables"]) == len(result["objects"]) == len(result["events"]) == 2
        assert {row["filename"] for row in result["tables"]} == {"Primary.htm", "release.htm"}
        requested = [entry["url"] for entry in result["trace"] if entry["kind"] == "sec"]
        assert not any(url.endswith(("graphic.jpg", "Slides.PDF")) for url in requested)
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


@pytest.mark.container
def test_installed_yahoo_factory_refreshes_with_private_caches_and_stops(docker):
    with fixture_container(docker, "yahoo") as name:
        def ready():
            health = read_json(docker, name, "/tmp/finbot-ingestion-health.json")
            return health if health and health["provider_configured"] and not health["calendar_stale"] else None
        health = until(ready, docker, name)
        assert health["calendar_coverage_start"] and health["calendar_coverage_end"]
        assert docker("exec", name, "stat", "-c", "%a:%u", "/tmp/finbot-yahoo").stdout.strip() == "700:10001"
        requests = sum(t["kind"] == "yahoo" for t in trace(docker, name))
        assert requests >= 62  # every day verified, plus nonempty terminal pages
        docker("kill", "--signal", "SIGTERM", name)
        assert docker("wait", name).stdout.strip() == "0", docker("logs", name).stderr
        result = final_result(docker, name)
        kinds = [t["kind"] for t in result["trace"]]
        assert kinds.index("yahoo_closed") < kinds.index("client_closed")
        assert sum(t["kind"] == "yahoo" for t in result["trace"]) == requests


@pytest.mark.container
def test_installed_yahoo_shutdown_waits_for_http_without_new_admissions(docker):
    with fixture_container(docker, "yahoo_blocking") as name:
        until(lambda:any(t["kind"] == "yahoo_blocked" for t in trace(docker, name)), docker, name)
        docker("kill", "--signal", "SIGTERM", name)
        until(lambda:any(t["kind"] == "stop" for t in trace(docker, name)), docker, name)
        assert not any(t["kind"] == "yahoo_closed" for t in trace(docker, name))
        count = sum(t["kind"] == "yahoo" for t in trace(docker, name))
        docker("exec", name, "touch", "/tmp/fixture/release")
        assert docker("wait", name).stdout.strip() == "0", docker("logs", name).stderr
        result = final_result(docker, name)
        kinds = [t["kind"] for t in result["trace"]]
        assert kinds.index("yahoo_released") < kinds.index("yahoo_closed") < kinds.index("client_closed")
        assert sum(t["kind"] == "yahoo" for t in result["trace"]) == count
