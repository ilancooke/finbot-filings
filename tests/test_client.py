from __future__ import annotations

from typing import Any

import pytest
import requests

from finbot_filings.sec.client import (
    SECClient,
    SECConfigurationError,
    SECHTTPError,
    SECNetworkError,
)


class FakeHeaders(dict[str, str]):
    pass


class FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        payload: Any = None,
        content: bytes = b"",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = content

    def json(self) -> Any:
        return self._payload


class FakeSession:
    def __init__(self, outcome: FakeResponse | Exception) -> None:
        self.headers = FakeHeaders()
        self.outcome = outcome
        self.calls: list[tuple[str, float]] = []

    def get(self, url: str, *, timeout: float) -> FakeResponse:
        self.calls.append((url, timeout))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_requires_identifying_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.setenv("FINBOT_FILINGS_CONFIG", "/path/that/does/not/exist")
    with pytest.raises(SECConfigurationError, match="SEC_USER_AGENT"):
        SECClient()


def test_reads_user_agent_from_config_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "filings.env"
    config_file.write_text(
        "SEC_USER_AGENT=Finbot config-contact@example.com\n", encoding="utf-8"
    )
    monkeypatch.setenv("FINBOT_FILINGS_CONFIG", str(config_file))
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    session = FakeSession(FakeResponse(content=b"ok"))
    SECClient(session=session, requests_per_second=None)
    assert session.headers["User-Agent"] == "Finbot config-contact@example.com"


def test_configures_headers_and_caches_json() -> None:
    session = FakeSession(FakeResponse(payload={"hello": "sec"}))
    client = SECClient(
        "Finbot owner@example.com", session=session, requests_per_second=None
    )
    first = client.get_json("https://example.test/data.json")
    second = client.get_json("https://example.test/data.json")
    assert first == second == {"hello": "sec"}
    assert session.headers["User-Agent"] == "Finbot owner@example.com"
    assert session.calls == [("https://example.test/data.json", 30.0)]


def test_get_bytes_preserves_response_bytes() -> None:
    source = b"\x00<html>original\r\nbytes</html>\xff"
    session = FakeSession(FakeResponse(content=source))
    client = SECClient("Finbot owner@example.com", session=session, requests_per_second=None)
    assert client.get_bytes("https://example.test/filing.htm") == source


def test_http_error_handling() -> None:
    session = FakeSession(FakeResponse(status_code=429))
    client = SECClient("Finbot owner@example.com", session=session, requests_per_second=None)
    with pytest.raises(SECHTTPError, match="HTTP 429"):
        client.get_bytes("https://example.test/filing.htm")


def test_network_error_handling() -> None:
    session = FakeSession(requests.ConnectionError("connection failed"))
    client = SECClient("Finbot owner@example.com", session=session, requests_per_second=None)
    with pytest.raises(SECNetworkError, match="connection failed"):
        client.get_bytes("https://example.test/filing.htm")


def test_default_rate_limit_waits_between_requests() -> None:
    times = iter([1.0, 1.02, 1.10])
    sleeps: list[float] = []
    session = FakeSession(FakeResponse(content=b"ok"))
    client = SECClient(
        "Finbot owner@example.com",
        session=session,
        clock=lambda: next(times),
        sleeper=sleeps.append,
    )
    client.get_bytes("https://example.test/one")
    client.get_bytes("https://example.test/two")
    assert sleeps == pytest.approx([0.08])
