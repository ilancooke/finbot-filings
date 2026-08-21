"""Configuration loading for finbot-filings."""

from __future__ import annotations

import os
from pathlib import Path

CONFIG_FILE_ENV = "FINBOT_FILINGS_CONFIG"
DOWNLOAD_FOLDER_ENV = "DOWNLOAD_FOLDER"
SEC_USER_AGENT_ENV = "SEC_USER_AGENT"
PROJECT_CONFIG_FILE = Path(__file__).resolve().parents[2] / ".env"


class ConfigurationError(RuntimeError):
    """Raised when required application configuration is unavailable."""


def _config_file() -> Path:
    configured_path = os.getenv(CONFIG_FILE_ENV)
    if configured_path:
        return Path(configured_path).expanduser()
    working_directory_file = Path.cwd() / ".env"
    if working_directory_file.is_file():
        return working_directory_file
    return PROJECT_CONFIG_FILE


def _read_setting(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            return value.strip().strip("'\"")
    return None


def _required_setting(key: str) -> str:
    value = os.getenv(key)
    config_path = _config_file()
    if value is None:
        value = _read_setting(config_path, key)
    if value is None or not value.strip():
        raise ConfigurationError(
            f"Set {key} in the environment or in {config_path}."
        )
    return value.strip()


def download_folder() -> Path:
    """Resolve DOWNLOAD_FOLDER from the environment or package config file."""
    return Path(_required_setting(DOWNLOAD_FOLDER_ENV)).expanduser()


def sec_user_agent() -> str:
    """Resolve SEC_USER_AGENT from the environment or package config file."""
    return _required_setting(SEC_USER_AGENT_ENV)
