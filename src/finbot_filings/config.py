"""Configuration loading for finbot-filings."""

from __future__ import annotations

import os
import re
from pathlib import Path

CONFIG_FILE_ENV = "FINBOT_FILINGS_CONFIG"
DOWNLOAD_FOLDER_ENV = "DOWNLOAD_FOLDER"
SECTION_FOLDER_ENV = "SECTION_FOLDER"
XBRL_FOLDER_ENV = "XBRL_FOLDER"
SEC_USER_AGENT_ENV = "SEC_USER_AGENT"
SEC_CIK_OVERRIDES_ENV = "SEC_CIK_OVERRIDES"
PROJECT_CONFIG_FILE = Path(__file__).resolve().parents[2] / ".env"
TICKER_PATTERN = re.compile(r"^[A-Z0-9.-]+$")


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
    value = _optional_setting(key)
    config_path = _config_file()
    if value is None or not value.strip():
        raise ConfigurationError(
            f"Set {key} in the environment or in {config_path}."
        )
    return value.strip()


def _optional_setting(key: str) -> str | None:
    value = os.getenv(key)
    if value is not None:
        return value
    return _read_setting(_config_file(), key)


def download_folder() -> Path:
    """Resolve DOWNLOAD_FOLDER from the environment or package config file."""
    return Path(_required_setting(DOWNLOAD_FOLDER_ENV)).expanduser()


def section_folder() -> Path:
    """Resolve SECTION_FOLDER from the environment or package config file."""
    return Path(_required_setting(SECTION_FOLDER_ENV)).expanduser()


def xbrl_folder() -> Path:
    """Resolve XBRL_FOLDER from the environment or package config file."""
    return Path(_required_setting(XBRL_FOLDER_ENV)).expanduser()


def sec_user_agent() -> str:
    """Resolve SEC_USER_AGENT from the environment or package config file."""
    return _required_setting(SEC_USER_AGENT_ENV)


def sec_cik_overrides() -> dict[str, int]:
    """Return optional ticker-to-CIK continuity overrides from configuration."""
    value = _optional_setting(SEC_CIK_OVERRIDES_ENV)
    if value is None or not value.strip():
        return {}

    overrides: dict[str, int] = {}
    for raw_entry in value.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise ConfigurationError(
                f"Invalid {SEC_CIK_OVERRIDES_ENV} entry {entry!r}; "
                "expected TICKER:CIK."
            )
        raw_ticker, raw_cik = entry.split(":", 1)
        ticker = raw_ticker.strip().upper()
        try:
            cik = int(raw_cik.strip())
        except ValueError as exc:
            raise ConfigurationError(
                f"Invalid CIK in {SEC_CIK_OVERRIDES_ENV} entry {entry!r}."
            ) from exc
        if not TICKER_PATTERN.fullmatch(ticker) or not 0 < cik <= 9_999_999_999:
            raise ConfigurationError(
                f"Invalid {SEC_CIK_OVERRIDES_ENV} entry {entry!r}."
            )
        overrides[ticker] = cik
    return overrides
