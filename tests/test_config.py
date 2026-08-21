from __future__ import annotations

from pathlib import Path

import pytest

from finbot_filings.config import ConfigurationError, download_folder, sec_user_agent


def test_download_folder_reads_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "filings.env"
    config_file.write_text("DOWNLOAD_FOLDER=/data/raw/filings/\n", encoding="utf-8")
    monkeypatch.setenv("FINBOT_FILINGS_CONFIG", str(config_file))
    monkeypatch.delenv("DOWNLOAD_FOLDER", raising=False)
    assert download_folder() == Path("/data/raw/filings")


def test_environment_overrides_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "filings.env"
    config_file.write_text("DOWNLOAD_FOLDER=/from/config\n", encoding="utf-8")
    monkeypatch.setenv("FINBOT_FILINGS_CONFIG", str(config_file))
    monkeypatch.setenv("DOWNLOAD_FOLDER", "/from/environment")
    assert download_folder() == Path("/from/environment")


def test_sec_user_agent_reads_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "filings.env"
    config_file.write_text(
        "DOWNLOAD_FOLDER=/data/raw/filings\n"
        "SEC_USER_AGENT=Finbot contact@example.com\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FINBOT_FILINGS_CONFIG", str(config_file))
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    assert sec_user_agent() == "Finbot contact@example.com"


def test_missing_download_folder_is_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FINBOT_FILINGS_CONFIG", str(tmp_path / "missing.env"))
    monkeypatch.delenv("DOWNLOAD_FOLDER", raising=False)
    with pytest.raises(ConfigurationError, match="DOWNLOAD_FOLDER"):
        download_folder()
