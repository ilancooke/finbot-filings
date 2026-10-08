import pytest

from finbot_ingestion.config import ConfigurationError, IngestionConfig


def test_env_config_needs_no_legacy_folders_or_aws_settings(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Finbot owner@example.com")
    monkeypatch.delenv("SEC_MAX_REQUESTS_PER_SECOND", raising=False)
    settings = IngestionConfig.from_env()
    assert settings.sec_max_requests_per_second == 5
    assert IngestionConfig.from_env({"SEC_USER_AGENT": " Finbot owner@example.com "}) == settings
    assert IngestionConfig.from_env({"SEC_USER_AGENT": "Finbot owner@example.com", "SEC_MAX_REQUESTS_PER_SECOND": "2.5"}).sec_max_requests_per_second == 2.5


@pytest.mark.parametrize("rate", ["0", "-1", "5.01", "10", "nan", "inf", "", "disabled"])
def test_invalid_or_excessive_rate_cannot_disable_ceiling(rate):
    with pytest.raises(ConfigurationError, match="SEC_MAX_REQUESTS_PER_SECOND"):
        IngestionConfig.from_env({"SEC_USER_AGENT": "Finbot owner@example.com", "SEC_MAX_REQUESTS_PER_SECOND": rate})


@pytest.mark.parametrize("agent", ["", "   ", "Finbot\r\nOther: header"])
def test_missing_or_invalid_user_agent(agent):
    with pytest.raises(ConfigurationError, match="SEC_USER_AGENT"):
        IngestionConfig.from_env({"SEC_USER_AGENT": agent})


def test_empty_injected_environment_does_not_fall_back_to_process(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "Finbot owner@example.com")
    with pytest.raises(ConfigurationError, match="SEC_USER_AGENT"):
        IngestionConfig.from_env({})


@pytest.mark.parametrize("rate", [True, None, float("nan"), 6])
def test_direct_construction_also_validates_rate(rate):
    with pytest.raises(ConfigurationError):
        IngestionConfig("Finbot owner@example.com", rate)
