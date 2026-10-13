"""Offline module commands and injected application entrypoint behavior."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sys

import pytest

from tests.support.container import run, command_environment


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


def test_main_entrypoint_and_health_check_without_aws(monkeypatch, tmp_path):
    from finbot_ingestion.main import main
    from finbot_ingestion.observability.health import HealthFile
    monkeypatch.setenv("RUNTIME_HEALTH_PATH", str(tmp_path / "health.json"))
    assert main(["--health-check"]) == 1
    health = HealthFile(tmp_path / "health.json")
    from datetime import datetime, timezone
    health.write({"live": True, "heartbeat_at": datetime.now(timezone.utc).isoformat()})
    assert main(["--health-check"]) == 0
    class FakeApp:
        def request_stop(self):
            pass
        async def run(self):
            return
    assert main([], builder=lambda stack: FakeApp()) == 0
    def broken(stack):
        raise ValueError("credential-bearing URL must not be printed")
    assert main([], builder=broken) == 1
