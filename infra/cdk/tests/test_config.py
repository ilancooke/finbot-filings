"""Public deployment settings and private identities stay separate and offline."""
import json
from pathlib import Path

import pytest

from config import InfraConfig, PRIVATE_SETTINGS

CONFIG_DIR = Path(__file__).parents[1]


def write_json(tmp_path, name, values):
    path = tmp_path / name
    path.write_text(json.dumps(values))
    return path


def private_values():
    return json.loads((CONFIG_DIR / "config.private.example.json").read_text())


def test_production_settings_with_fixture_private_inputs():
    public = json.loads((CONFIG_DIR / "config.prod.json").read_text())
    assert not PRIVATE_SETTINGS.intersection(public)
    config = InfraConfig.read(CONFIG_DIR / "config.prod.json",
        private_path=CONFIG_DIR / "config.private.example.json")
    assert config.name == "finbot-prod"
    assert config.account == "123456789012"
    assert config.alarm_email == "your-email@example.com"
    assert config.monitoring_enabled and config.calendar_provider == "yahoo"
    assert config.live_ingestion_start_at == "2026-10-06T04:00:00Z"
    assert config.image_digest == public["image_digest"]


def test_examples_mirror_production_file_structure():
    example = json.loads((CONFIG_DIR / "config.example.json").read_text())
    production = json.loads((CONFIG_DIR / "config.prod.json").read_text())
    assert example.keys() == production.keys()
    assert not PRIVATE_SETTINGS.intersection(example)
    assert private_values().keys() == PRIVATE_SETTINGS
    config = InfraConfig.read(CONFIG_DIR / "config.example.json",
        private_path=CONFIG_DIR / "config.private.example.json")
    assert config.name == "finbot-example" and config.account == "123456789012"
    assert config.calendar_provider == production["calendar_provider"]
    assert config.monitoring_enabled == production["monitoring_enabled"]


@pytest.mark.parametrize("setting", ["live_ingestion_start_at", "image_digest", "region"])
def test_private_file_cannot_override_public_deployment_settings(tmp_path, setting):
    private = private_values()
    private[setting] = "unexpected override"
    path = write_json(tmp_path, "private.json", private)
    with pytest.raises(ValueError, match="private configuration cannot contain deployment settings"):
        InfraConfig.read(CONFIG_DIR / "config.prod.json", private_path=path)


@pytest.mark.parametrize("setting", sorted(PRIVATE_SETTINGS))
def test_private_settings_cannot_be_in_public_file_when_split(tmp_path, setting):
    public = json.loads((CONFIG_DIR / "config.prod.json").read_text())
    public[setting] = private_values()[setting]
    path = write_json(tmp_path, "public.json", public)
    with pytest.raises(ValueError, match="put these settings in the private configuration"):
        InfraConfig.read(path, private_path=CONFIG_DIR / "config.private.example.json")


@pytest.mark.parametrize("setting", ["account", "sec_user_agent"])
def test_private_file_requires_account_and_sec_contact(tmp_path, setting):
    private = private_values()
    private.pop(setting)
    path = write_json(tmp_path, "private.json", private)
    with pytest.raises(ValueError, match="private configuration is missing"):
        InfraConfig.read(CONFIG_DIR / "config.prod.json", private_path=path)


def test_missing_private_file_fails_without_fallback(tmp_path):
    with pytest.raises(FileNotFoundError):
        InfraConfig.read(CONFIG_DIR / "config.prod.json", private_path=tmp_path / "missing.json")


@pytest.mark.parametrize("private_file", [False, True])
def test_configuration_requires_json_objects(tmp_path, private_file):
    path = write_json(tmp_path, "invalid.json", [])
    with pytest.raises(ValueError, match="configuration must be a JSON object"):
        if private_file:
            InfraConfig.read(CONFIG_DIR / "config.prod.json", private_path=path)
        else:
            InfraConfig.read(path)


def test_standalone_complete_input_remains_supported(tmp_path):
    values = json.loads((CONFIG_DIR / "config.example.json").read_text())
    values.update(private_values())
    config = InfraConfig.read(write_json(tmp_path, "complete.json", values))
    assert config.name == "finbot-example"
    assert config.account == "123456789012"
