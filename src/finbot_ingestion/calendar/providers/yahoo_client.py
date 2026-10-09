"""Pinned yfinance raw authentication seam with an owned, bounded HTTP session."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from importlib.metadata import version
import logging
import math
import os
from pathlib import Path
import threading
import time
from urllib.parse import urljoin, urlsplit

from curl_cffi import requests

from finbot_ingestion.config import ConfigurationError
from ..provider import CalendarTransientError, IncompleteCalendarSnapshot
from .yahoo_parser import query_body

URL = "https://query1.finance.yahoo.com/v1/finance/visualization"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
OWNER = threading.Lock()
LOGGER = logging.getLogger(__name__)


class YahooStopped(Exception):
    """Shutdown closes admission; this is not a provider failure."""


class YahooBackoff(CalendarTransientError):
    def __init__(self, delay):
        super().__init__("Yahoo request unavailable")
        self.delay = delay


class FetchBudget:
    def __init__(self, config, stop, *, monotonic=time.monotonic):
        self.config, self.stop, self.monotonic = config, stop, monotonic
        self.deadline = monotonic() + config.max_fetch_seconds
        self.attempts = self.rows = self.pages = 0

    def remaining(self):
        if self.stop.is_set():
            raise YahooStopped()
        remaining = self.deadline - self.monotonic()
        if remaining <= 0:
            raise IncompleteCalendarSnapshot("Yahoo fetch deadline exhausted")
        return remaining

    def wait(self, seconds):
        remaining = self.remaining()
        if seconds >= remaining:
            raise IncompleteCalendarSnapshot("Yahoo fetch deadline exhausted")
        if self.stop.wait(max(0, seconds)):
            raise YahooStopped()
        self.remaining()


class YahooSession(requests.Session):
    def __init__(self, config):
        super().__init__(impersonate="chrome", trust_env=False)
        self.config = config
        self.budget = None
        self.last_start = None
        self.metrics = None

    def _perform(self, method, url, **kwargs):
        return super().request(method, url, **kwargs)

    def request(self, method, url, **kwargs):
        budget = self.budget
        if budget is None:
            raise RuntimeError("Yahoo HTTP requires an active fetch budget")
        kwargs["allow_redirects"] = False
        # Each hop, including auth redirects, is explicitly admitted and counted.
        for hop in range(6):
            parts = urlsplit(url)
            if parts.scheme != "https" or not parts.hostname or not parts.hostname.endswith(".yahoo.com") or parts.username or parts.password or parts.port not in (None, 443):
                raise IncompleteCalendarSnapshot("unsafe Yahoo redirect/request")
            if self.last_start is not None:
                budget.wait(max(0, self.last_start + self.config.min_request_interval_seconds - budget.monotonic()))
            remaining = budget.remaining()
            if budget.attempts >= self.config.max_http_attempts:
                raise IncompleteCalendarSnapshot("Yahoo HTTP attempt budget exhausted")
            budget.attempts += 1
            if self.metrics is not None:
                self.metrics.count("YahooHttpAttempts")
            self.last_start = budget.monotonic()
            kwargs["timeout"] = min(self.config.http_timeout_seconds, remaining)
            try:
                response = self._perform(method, url, **kwargs)
            except (requests.exceptions.RequestException, OSError):
                LOGGER.warning("Yahoo HTTP failed", extra={"operation": "yahoo_http", "attempt_number": budget.attempts,
                    "error_type": "TransportError", "will_retry": True})
                raise YahooBackoff(1) from None
            budget.remaining()
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise IncompleteCalendarSnapshot("Yahoo response exceeds size bound")
            if response.status_code in (301, 302, 303, 307, 308):
                if hop == 5 or not response.headers.get("Location"):
                    raise IncompleteCalendarSnapshot("Yahoo redirect bound exhausted")
                url = urljoin(url, response.headers["Location"])
                kwargs.pop("params", None)
                if response.status_code == 303 or (response.status_code in (301, 302) and method.upper() == "POST"):
                    method = "GET"
                    for name in ("json", "data", "headers"):
                        kwargs.pop(name, None)
                continue
            if response.status_code in (403, 429) or response.status_code >= 500:
                LOGGER.warning("Yahoo HTTP unavailable", extra={"operation": "yahoo_http", "attempt_number": budget.attempts,
                    "http_status": response.status_code, "will_retry": True})
                if self.metrics is not None:
                    self.metrics.count("YahooHttpErrors")
                delay = 1.0
                value = response.headers.get("Retry-After")
                if value:
                    try:
                        delay = float(value)
                    except ValueError:
                        try:
                            delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
                        except (ValueError, TypeError, OverflowError):
                            pass
                if not math.isfinite(delay):
                    delay = self.config.backoff_cap_seconds
                raise YahooBackoff(min(self.config.backoff_cap_seconds, max(1, delay)))
            return response
        raise IncompleteCalendarSnapshot("Yahoo redirect bound exhausted")


class YahooPageClient:
    """One process owner. Initialization and cache close run on its worker thread."""

    def __init__(self, config):
        self.config = config
        self.session = self.data = None
        self._owned = False
        self.metrics = None

    def _initialize(self):
        if self.session is not None:
            return
        if version("yfinance") != "1.7.0":
            raise ConfigurationError("Yahoo raw seam requires yfinance 1.7.0")
        if not OWNER.acquire(blocking=False):
            raise ConfigurationError("only one Yahoo cache/session owner is supported")
        self._owned = True
        try:
            from yfinance import cache
            from yfinance.data import YfData
            # The installed helper logs auth-bearing values at debug level. Never
            # forward its logs, irrespective of application LOG_LEVEL.
            logger = logging.getLogger("yfinance")
            logger.disabled = True
            logger.propagate = False
            directory = Path(self.config.cache_dir)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.is_symlink() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
                raise ConfigurationError("Yahoo cache directory must be private and owned by the runtime user")
            cache.set_cache_location(str(directory))
            self.session = YahooSession(self.config)
            self.session.metrics = self.metrics
            # Bypass YfData's singleton metaclass, keeping auth facts/session owned
            # here. No functions or global HTTP transports are monkeypatched.
            self.data = object.__new__(YfData)
            YfData.__init__(self.data, session=self.session)
        except BaseException:
            self.close()
            raise

    def page(self, day, size, offset, budget):
        budget.remaining()
        self._initialize()
        self.session.budget = budget
        body = query_body(day, size, offset)
        for attempt in range(self.config.request_attempts):
            budget.remaining()
            try:
                response = self.data.post(URL, body=body, params={"lang": "en-US", "region": "US"},
                                          timeout=self.config.http_timeout_seconds)
                if response.status_code != 200:
                    raise IncompleteCalendarSnapshot("Yahoo calendar HTTP request failed")
                payload = response.json()
                budget.remaining()
                budget.pages += 1
                if attempt:
                    LOGGER.info("Yahoo HTTP recovered", extra={"operation": "yahoo_http", "attempt_number": attempt + 1})
                return payload
            except YahooBackoff as exc:
                if attempt + 1 == self.config.request_attempts:
                    raise CalendarTransientError("Yahoo request retry budget exhausted") from None
                if self.metrics is not None:
                    self.metrics.count("RetryAttempts")
                budget.wait(exc.delay)
            except (ValueError, TypeError):
                raise IncompleteCalendarSnapshot("Yahoo response is not valid JSON") from None
            except (YahooStopped, CalendarTransientError, IncompleteCalendarSnapshot, ConfigurationError):
                raise
            except Exception:
                raise CalendarTransientError("Yahoo transport/authentication unavailable") from None

    def close(self):
        try:
            if self.session is not None:
                self.session.close()
        finally:
            self.session = self.data = None
            if self._owned:
                try:
                    from yfinance import cache
                    # Close SQLite on the same dedicated thread that used it.
                    cache._CookieDBManager.close_db()
                    cache._TzDBManager.close_db()
                finally:
                    self._owned = False
                    OWNER.release()
