"""Bounded metrics, sanitized logs and local heartbeat health."""

from datetime import timedelta
import json
import logging
import threading

import pytest

from finbot_ingestion.observability.health import HealthFile
from finbot_ingestion.observability.logging import JSONFormatter
from finbot_ingestion.observability.metrics import Metrics
from finbot_ingestion.runtime.config import RuntimeConfig

from tests.support.scheduling import NOW


def test_environment_types_telemetry_bounds_and_sanitization(tmp_path):
    config = RuntimeConfig.from_env({"RUNTIME_POLL_WORKERS": "3", "RUNTIME_TICK_SECONDS": "0.5"})
    assert config.poll_workers == 3 and config.tick_seconds == .5
    output = []
    metrics = Metrics(sink=output.append)
    threads = [threading.Thread(target=lambda: [metrics.count("Requests") for _ in range(100)]) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for value in range(300):
        metrics.observe("Latency", value)
    metrics.flush()
    record = json.loads(output[0])
    assert record["Requests"] == 400 and len(record["Latency"]) == 100 and record["MetricSamplesDropped"] == 200
    assert record["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["Service", "Environment"]]
    with pytest.raises(ValueError):
        metrics.observe("Bad", float("inf"))
    log = logging.LogRecord("finbot_ingestion.test", logging.WARNING, "test", 1, "Provider failed", (), None)
    log.error_message, log.raw_payload = "secret", {"token": "secret"}
    assert "secret" not in JSONFormatter().format(log)
    path = tmp_path / "health.json"
    health = HealthFile(path)
    health.write({"heartbeat_at": NOW.isoformat(), "live": True})
    assert HealthFile.check(path, now=NOW)
    assert not HealthFile.check(path, now=NOW + timedelta(seconds=100))
    health.remove()
    assert not path.exists()
