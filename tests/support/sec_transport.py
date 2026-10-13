"""Reusable offline sec transport test support."""

from finbot_ingestion.config import IngestionConfig
from finbot_ingestion.sec.client import SecClient
from finbot_ingestion.sec.rate_limiter import SECRateLimiter


URL = 'https://www.sec.gov/Archives/test'


class Clock:
    def __init__(self):
        self.value = 0.0
    def __call__(self):
        return self.value
    def sleep(self, seconds):
        self.value += seconds


class Response:
    def __init__(self, content=b'raw\x00\xff\r\n', status=200, headers=None):
        self.content, self.status_code = content, status
        self.headers = headers or {}
        self.closed = False
    def close(self):
        self.closed = True


class Session:
    def __init__(self, clock, outcomes):
        self.headers, self.calls = {}, []
        self.clock, self.outcomes = clock, list(outcomes)
        self.closed = False
    def mount(self, prefix, adapter):
        assert adapter.max_retries.total == 0
    def get(self, url, **kwargs):
        self.calls.append((self.clock(), url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    def close(self):
        self.closed = True


def make(outcomes, rate=5, **options):
    clock = Clock()
    session = Session(clock, outcomes)
    limiter = SECRateLimiter(rate, clock=clock, sleeper=clock.sleep)
    client = SecClient(IngestionConfig('Finbot owner@example.com', rate, **options), limiter=limiter, session=session, sleeper=clock.sleep, random_value=lambda: 0.5)
    return client, session, clock


class StreamingResponse:
    def __init__(self, chunks, headers=None):
        self.chunks, self.headers = chunks, headers or {}
        self.status_code, self.closed, self.read_chunks = 200, False, 0

    @property
    def content(self):
        raise AssertionError("bounded downloads must not materialize response.content")

    def iter_content(self, chunk_size):
        assert chunk_size == 64 * 1024
        for chunk in self.chunks:
            self.read_chunks += 1
            yield chunk

    def close(self):
        self.closed = True
