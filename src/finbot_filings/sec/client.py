"""Reusable HTTP client for official SEC public-data endpoints."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import requests

from finbot_filings.config import (
    SEC_USER_AGENT_ENV,
    ConfigurationError,
    sec_user_agent,
)

LOGGER = logging.getLogger(__name__)
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_REQUESTS_PER_SECOND = 10.0


class SECError(RuntimeError):
    """Base class for SEC access failures."""


class SECConfigurationError(SECError):
    """Raised when required SEC client configuration is missing."""


class SECNetworkError(SECError):
    """Raised when an SEC request cannot reach the server."""


class SECHTTPError(SECError):
    """Raised when an SEC endpoint returns an unsuccessful HTTP response."""


class SECDataError(SECError):
    """Raised when an SEC response has an unexpected shape or encoding."""


class SECClient:
    """Small SEC client with identification, throttling, and JSON caching."""

    def __init__(
        self,
        user_agent: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        requests_per_second: float | None = DEFAULT_REQUESTS_PER_SECOND,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        try:
            configured_user_agent = (
                user_agent.strip() if user_agent else sec_user_agent()
            )
        except ConfigurationError as exc:
            raise SECConfigurationError(
                f"Set {SEC_USER_AGENT_ENV} to an identifying value such as "
                "'Organization contact@example.com'."
            ) from exc
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if requests_per_second is not None and requests_per_second <= 0:
            raise ValueError("requests_per_second must be greater than zero")

        self._timeout = timeout
        self._minimum_interval = (
            1.0 / requests_per_second if requests_per_second is not None else 0.0
        )
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "User-Agent": configured_user_agent,
                "Accept-Encoding": "gzip, deflate",
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
            }
        )
        self._clock = clock
        self._sleeper = sleeper
        self._last_request_started: float | None = None
        self._rate_lock = threading.Lock()
        self._json_cache: dict[str, Any] = {}

    def get_json(self, url: str) -> Any:
        """Fetch and decode JSON, caching successful responses by URL."""
        if url in self._json_cache:
            return self._json_cache[url]
        response = self._request(url)
        try:
            payload = response.json()
        except (requests.JSONDecodeError, ValueError) as exc:
            raise SECDataError(f"SEC returned invalid JSON for {url}") from exc
        self._json_cache[url] = payload
        return payload

    def get_bytes(self, url: str) -> bytes:
        """Fetch response bytes without transforming them."""
        return self._request(url).content

    def _request(self, url: str) -> requests.Response:
        self._wait_for_rate_limit()
        LOGGER.debug("Requesting SEC URL %s", url)
        try:
            response = self._session.get(url, timeout=self._timeout)
        except requests.RequestException as exc:
            raise SECNetworkError(f"Unable to request SEC URL {url}: {exc}") from exc
        if not 200 <= response.status_code < 300:
            raise SECHTTPError(
                f"SEC request failed with HTTP {response.status_code} for {url}"
            )
        return response

    def _wait_for_rate_limit(self) -> None:
        with self._rate_lock:
            now = self._clock()
            if self._last_request_started is not None:
                wait_seconds = self._minimum_interval - (now - self._last_request_started)
                if wait_seconds > 0:
                    self._sleeper(wait_seconds)
                    now = self._clock()
            self._last_request_started = now
