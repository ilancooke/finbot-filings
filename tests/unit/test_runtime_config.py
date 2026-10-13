"""Runtime settings, bounds and environment parsing."""

import pytest

from finbot_ingestion.config import ConfigurationError
from finbot_ingestion.runtime.config import RuntimeConfig


@pytest.mark.parametrize("changes", [{"active_poll_seconds": float("nan")}, {"grace_seconds": 0},
    {"poll_workers": True}, {"market_timezone": "absent"}, {"non_session_end_hour": 1},
    {"active_poll_seconds": 4000}, {"health_path": "relative"}])
def test_runtime_config_validation(changes):
    with pytest.raises(ConfigurationError):
        RuntimeConfig(**changes)


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan"), True])
def test_quiet_period_rejects_invalid_values(value):
    with pytest.raises(ConfigurationError):
        RuntimeConfig(sec_startup_quiet_seconds=value)


def test_runtime_environment_and_local_default():
    assert RuntimeConfig().sec_startup_quiet_seconds == 0
    config = RuntimeConfig.from_env({"RUNTIME_SEC_STARTUP_QUIET_SECONDS": "150", "RUNTIME_METRICS_ENVIRONMENT": "production"})
    assert config.sec_startup_quiet_seconds == 150 and config.metrics_environment == "production"
    with pytest.raises(ConfigurationError):
        RuntimeConfig(metrics_environment="unsafe space")
