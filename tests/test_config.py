from __future__ import annotations

from pathlib import Path

import pytest

from finbot_filings.config import (
    ConfigurationError,
    download_folder,
    sec_cik_overrides,
    sec_user_agent,
    section_folder,
)


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


def test_section_folder_reads_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "filings.env"
    config_file.write_text("SECTION_FOLDER=/data/filings/sections/\n", encoding="utf-8")
    monkeypatch.setenv("FINBOT_FILINGS_CONFIG", str(config_file))
    monkeypatch.delenv("SECTION_FOLDER", raising=False)
    assert section_folder() == Path("/data/filings/sections")


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


def test_sec_cik_overrides_parse_multiple_tickers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "filings.env"
    config_file.write_text(
        "SEC_CIK_OVERRIDES=XOM:34088, example:123456\n", encoding="utf-8"
    )
    monkeypatch.setenv("FINBOT_FILINGS_CONFIG", str(config_file))
    monkeypatch.delenv("SEC_CIK_OVERRIDES", raising=False)
    assert sec_cik_overrides() == {"XOM": 34088, "EXAMPLE": 123456}


@pytest.mark.parametrize("value", ["XOM", "XOM:not-a-number", "XOM:0"])
def test_invalid_sec_cik_override_is_clear(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEC_CIK_OVERRIDES", value)
    with pytest.raises(ConfigurationError, match="SEC_CIK_OVERRIDES"):
        sec_cik_overrides()
