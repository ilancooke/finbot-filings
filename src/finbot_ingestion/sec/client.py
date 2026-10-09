"""Synchronous SEC transport with one shared serialized dispatch budget.

Later async callers must use a bounded executor. No automatic redirects/retries,
permanent cache, AWS calls, or scheduling live here.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import math
import random
import time
import threading
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter

from finbot_ingestion.domain import Company, Filing
from finbot_ingestion.config import IngestionConfig
from finbot_ingestion.ingestion.retry_policy import RetryPolicy
from .errors import SECDataError, SECDocumentTooLarge, SECHTTPError, SECNetworkError, SECRequestStopped
from .filing_index import FilingIndex, parse_filing_index
from .rate_limiter import SECRateLimiter
from .urls import accession_index_json_url, company_submissions_url

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DownloadedDocument:
    content: bytes
    content_type: str | None
    source_url: str

    @property
    def size_bytes(self) -> int:
        return len(self.content)


class SecClient:
    def __init__(self, config: IngestionConfig, *, limiter: SECRateLimiter,
                 session=None, sleeper=time.sleep, random_value=random.random,
                 now=lambda: datetime.now(timezone.utc)):
        if limiter.rate > config.sec_max_requests_per_second:
            raise ValueError('shared limiter exceeds configured rate')
        self.config, self.limiter = config, limiter
        self._session = session if session is not None else requests.Session()
        self._session.mount('https://', HTTPAdapter(max_retries=0))
        self._session.headers.update({'User-Agent': config.sec_user_agent,
                                     'Accept': 'application/json, text/html;q=0.9, */*;q=0.8',
                                     'Accept-Encoding': 'gzip, deflate'})
        self._sleep, self._random, self._now = sleeper, random_value, now
        self._retry = RetryPolicy(config.sec_max_attempts, config.sec_backoff_base_seconds, config.sec_backoff_cap_seconds)
        self._closed = False
        self._admissions_stopped = threading.Event()
        self.metrics = None

    def stop_admissions(self):
        # Never acquire the transport lock on the event loop: an in-flight request
        # may hold it for its entire response. Session close happens after cleanup.
        self._admissions_stopped.set()

    def _check_admission(self):
        if self._admissions_stopped.is_set():
            raise SECRequestStopped("SEC request admissions are stopped")

    def close(self) -> None:
        with self.limiter.dispatch_lock:
            self._closed = True
            self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @staticmethod
    def _validate_url(url):
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.hostname not in {'www.sec.gov', 'data.sec.gov'} or parts.port not in (None, 443) or parts.username or parts.password:
            raise SECDataError('requests must use official HTTPS SEC hosts')

    def _retry_after(self, value):
        if value is None:
            return None
        try:
            seconds = float(value)
            return seconds if math.isfinite(seconds) else None
        except ValueError:
            try:
                return (parsedate_to_datetime(value) - self._now()).total_seconds()
            except (ValueError, TypeError, OverflowError):
                return None

    def _request(self, url, *, operation, context=None, package=False, max_bytes=None):
        self._validate_url(url)
        context = context or {}
        redirects = 0
        attempt = 0
        had_failure = False
        while True:
            self._check_admission()
            attempt += 1
            response = None
            retry_after = None
            try:
                with self.limiter.dispatch_lock:
                    if self._closed:
                        raise RuntimeError('SEC client is closed')
                    self.limiter.wait()
                    self._check_admission()
                    if self.metrics is not None:
                        self.metrics.count("SecRequests")
                    options = {"stream": True} if max_bytes is not None else {}
                    response = self._session.get(url, timeout=(self.config.sec_connect_timeout_seconds, self.config.sec_read_timeout_seconds), allow_redirects=False, **options)
                    bounded_content = None
                    if max_bytes is not None and 200 <= response.status_code < 300:
                        length = response.headers.get('Content-Length')
                        if length is not None:
                            try:
                                length = int(length)
                            except (TypeError, ValueError) as exc:
                                raise SECDataError('invalid SEC Content-Length') from exc
                            if length < 0:
                                raise SECDataError('invalid SEC Content-Length')
                            if length > max_bytes:
                                raise SECDocumentTooLarge('SEC document exceeds MAX_ARTIFACT_BYTES')
                        content = bytearray()
                        for chunk in response.iter_content(chunk_size=64 * 1024):
                            if len(content) + len(chunk) > max_bytes:
                                raise SECDocumentTooLarge('SEC document exceeds MAX_ARTIFACT_BYTES')
                            content.extend(chunk)
                        bounded_content = bytes(content)
                status = response.status_code
                if self.metrics is not None:
                    if status in (403, 429):
                        self.metrics.count("SecThrottles")
                    if status >= 400:
                        self.metrics.count("SecRequestErrors")
                if status in (301, 302, 303, 307, 308):
                    if redirects >= self.config.sec_max_redirects or not response.headers.get('Location'):
                        raise SECDataError('invalid or exhausted SEC redirect chain')
                    target = urljoin(url, response.headers['Location'])
                    self._validate_url(target)
                    url = target
                    redirects += 1
                    attempt -= 1
                    continue
                if not 200 <= status < 300:
                    retryable = status in (403, 429) or 500 <= status <= 599 or (package and status == 404)
                    retry_after = self._retry_after(response.headers.get('Retry-After'))
                    error = SECHTTPError(status, url)
                else:
                    content = bounded_content if max_bytes is not None else response.content
                    if had_failure:
                        LOGGER.info('SEC request recovered', extra={'operation': operation, 'attempt_number': attempt, **context})
                    return DownloadedDocument(content, response.headers.get('Content-Type'), url)
            except (requests.Timeout, requests.ConnectionError) as exc:
                error, retryable = SECNetworkError(str(exc)), True
                if self.metrics is not None:
                    self.metrics.count("SecRequestErrors")
            except SECDataError as exc:
                error, retryable = exc, False
            except requests.RequestException as exc:
                error, retryable = SECNetworkError(str(exc)), False
            finally:
                if response is not None:
                    response.close()
            had_failure = True
            will_retry = retryable and attempt < self._retry.max_attempts
            LOGGER.warning('SEC request failed', extra={'operation': operation, 'attempt_number': attempt, 'error_type': type(error).__name__, 'error_message': str(error), 'will_retry': will_retry, **context})
            if not will_retry:
                raise error
            if self.metrics is not None:
                self.metrics.count("RetryAttempts")
            self._sleep(self._retry.delay(attempt, random_value=self._random, retry_after=retry_after))

    def get_company_submissions(self, company: Company, *, discovered_at: datetime | None = None) -> list[Filing]:
        return [observation.filing for observation in self.get_company_submissions_with_evidence(
            company, discovered_at=discovered_at)]

    def get_company_submissions_with_evidence(self, company, *, discovered_at=None):
        from .submissions import parse_company_submissions_with_evidence
        result = self._request(company_submissions_url(company.cik), operation='submissions', context={'cik': company.cik, 'ticker': company.ticker})
        try:
            return parse_company_submissions_with_evidence(company, self._json(result), discovered_at=discovered_at or self._now())
        except SECDataError as exc:
            LOGGER.warning('SEC submissions parsing failed', extra={'operation': 'submissions', 'cik': company.cik, 'ticker': company.ticker, 'error_type': type(exc).__name__, 'error_message': str(exc)})
            raise

    @staticmethod
    def _json(result):
        import json
        try:
            return json.loads(result.content)
        except (ValueError, UnicodeError) as exc:
            raise SECDataError('SEC returned invalid JSON') from exc

    def get_filing_index(self, filing: Filing) -> FilingIndex:
        context = {'cik': filing.company_cik, 'ticker': filing.ticker, 'accession_number': filing.accession_number}
        # Incomplete valid metadata is returned as an explicit retryable package error;
        # callers can retry the entire snapshot without multiplying HTTP retry loops.
        html = self._request(filing.filing_index_url, operation='filing_index', context=context, package=True)
        directory = self._request(accession_index_json_url(filing.company_cik, filing.accession_number), operation='filing_directory', context=context, package=True)
        try:
            return parse_filing_index(filing, html.content, self._json(directory))
        except SECDataError as exc:
            LOGGER.warning('SEC package enumeration failed', extra={'operation': 'package_enumeration', 'error_type': type(exc).__name__, 'error_message': str(exc), **context})
            raise

    def download_document(self, url: str, *, max_bytes: int | None = None) -> DownloadedDocument:
        if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 1):
            raise ValueError('max_bytes must be a positive integer')
        return self._request(url, operation='document', max_bytes=max_bytes)
