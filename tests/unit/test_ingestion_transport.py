import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
import requests

from finbot_ingestion.config import IngestionConfig, ConfigurationError
from finbot_ingestion.domain import Company, Filing
from finbot_ingestion.sec.client import SecClient
from finbot_ingestion.sec.errors import SECDataError, SECHTTPError, SECNetworkError
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

@pytest.mark.parametrize('rate', [5, 2.5, 1, 0.5])
def test_parallel_dispatch_has_no_bursts(rate):
    client, session, clock = make([Response() for _ in range(40)], rate)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: client.download_document(URL), range(20)))
        clock.value += 100
        list(executor.map(lambda _: client.download_document(URL), range(20)))
    starts = [c[0] for c in session.calls]
    assert all(b-a >= 1/rate-1e-9 for a,b in zip(starts, starts[1:]))
    assert all(sum(t <= x <= t+1 for x in starts) <= 5 for t in starts)
    assert all(call[2]['allow_redirects'] is False for call in session.calls)
    assert session.headers['User-Agent'] == 'Finbot owner@example.com'
    assert session.calls[0][2]['timeout'] == (10, 30)

def test_retries_redirects_and_exact_bytes_share_budget(caplog):
    responses = [Response(status=429, headers={'Retry-After':'0'}), Response(status=302, headers={'Location':'/Archives/next'}), Response()]
    client, session, _ = make(responses)
    with caplog.at_level('INFO'):
        result = client.download_document(URL)
    assert result.content == b'raw\x00\xff\r\n'
    assert result.size_bytes == 7
    assert len(session.calls) == 3
    assert all(r.closed for r in responses)
    assert any(r.message == 'SEC request recovered' for r in caplog.records)
    assert any(getattr(r, 'will_retry', False) for r in caplog.records)
    assert session.calls[-1][1].endswith('/Archives/next')

@pytest.mark.parametrize('outcome,error', [(Response(status=403), SECHTTPError),(Response(status=503), SECHTTPError),(requests.Timeout('timeout'),SECNetworkError),(requests.ConnectionError('reset'),SECNetworkError)])
def test_retry_exhaustion_is_bounded(outcome,error):
    client, session, _ = make([outcome]*3)
    with pytest.raises(error):
        client.download_document(URL)
    assert len(session.calls) == 3

@pytest.mark.parametrize('status', [400,401,404])
def test_terminal_status_does_not_retry(status):
    client, session, _ = make([Response(status=status)])
    with pytest.raises(SECHTTPError):
        client.download_document(URL)
    assert len(session.calls) == 1

def test_redirect_loop_and_external_target_are_rejected():
    client, session, _ = make([Response(status=302,headers={'Location':URL})]*3, sec_max_redirects=2)
    with pytest.raises(SECDataError):
        client.download_document(URL)
    assert len(session.calls)==3
    client, session, _ = make([Response(status=302,headers={'Location':'https://example.com'})])
    with pytest.raises(SECDataError):
        client.download_document(URL)
    assert len(session.calls)==1

def test_lifecycle_and_closed_client():
    client, session, _ = make([Response()])
    with client:
        client.download_document(URL)
    assert session.closed
    with pytest.raises(RuntimeError):
        client.download_document(URL)

def test_fresh_submissions():
    from pathlib import Path
    payload = json.loads((Path(__file__).parents[1]/'fixtures/submissions_mixed.json').read_text())
    empty = {'cik':320193, 'filings':{'recent':{'accessionNumber':[], 'form':[], 'acceptanceDateTime':[]}}}
    client, session, _ = make([Response(json.dumps(empty).encode()),Response(json.dumps(payload).encode())])
    company=Company(ticker='AAPL',cik='320193',name='Apple')
    assert client.get_company_submissions(company)==[]
    assert client.get_company_submissions(company)
    assert len(session.calls)==2

@pytest.mark.parametrize('options',[{'sec_max_attempts':0},{'sec_max_redirects':-1},{'sec_read_timeout_seconds':float('nan')},{'sec_backoff_cap_seconds':0.5}])
def test_transport_config_validation(options):
    with pytest.raises(ConfigurationError):
        IngestionConfig('Finbot owner@example.com', **options)

def test_lower_ceiling_cannot_be_bypassed_by_injected_limiter():
    with pytest.raises(ValueError):
        SecClient(IngestionConfig('Finbot owner@example.com',1),limiter=SECRateLimiter(5))

def test_retry_after_and_jitter_are_bounded():
    from finbot_ingestion.ingestion.retry_policy import RetryPolicy
    policy=RetryPolicy(3, 2, 10)
    assert policy.delay(1, random_value=lambda:0.5)==1
    assert policy.delay(30, random_value=lambda:1)==10
    assert policy.delay(1, random_value=lambda:0, retry_after=5)==5
    assert policy.delay(1, random_value=lambda:0, retry_after=100)==10
    client, _, _=make([])
    assert client._retry_after('nan') is None
    assert client._retry_after('invalid') is None
    assert client._retry_after('3')==3

def test_limiter_rechecks_after_early_wakeup():
    clock=Clock()
    sleeps=[]
    def early_sleep(seconds):
        sleeps.append(seconds)
        clock.sleep(seconds/2 if len(sleeps)==1 else seconds)
    limiter=SECRateLimiter(clock=clock,sleeper=early_sleep)
    with limiter.dispatch_lock:
        limiter.wait()
        limiter.wait()
    assert clock.value >= 0.2
    assert len(sleeps)==2

def test_two_clients_share_one_budget():
    clock=Clock()
    limiter=SECRateLimiter(clock=clock,sleeper=clock.sleep)
    sessions=[Session(clock,[Response()]*10) for _ in range(2)]
    clients=[SecClient(IngestionConfig('Finbot owner@example.com'),limiter=limiter,session=s) for s in sessions]
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda i:clients[i%2].download_document(URL),range(20)))
    starts=sorted(c[0] for s in sessions for c in s.calls)
    assert all(sum(t<=x<=t+1 for x in starts)<=5 for t in starts)

def test_package_404_is_retried_but_document_404_is_not():
    client, session, _=make([Response(status=404),Response(b'ok')])
    assert client._request(URL,operation='filing_index',package=True).content==b'ok'
    assert len(session.calls)==2

def test_transport_environment_options():
    config=IngestionConfig.from_env({'SEC_USER_AGENT':'Finbot owner@example.com','SEC_MAX_ATTEMPTS':'4','SEC_READ_TIMEOUT_SECONDS':'12.5','SEC_MAX_REDIRECTS':'0'})
    assert config.sec_max_attempts==4
    assert config.sec_read_timeout_seconds==12.5
    assert config.sec_max_redirects==0
    with pytest.raises(ConfigurationError):
        IngestionConfig.from_env({'SEC_USER_AGENT':'Finbot owner@example.com','SEC_MAX_ATTEMPTS':'2.5'})


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


def test_bounded_streaming_download_preserves_decoded_response_bytes():
    response = StreamingResponse([b"\x00\xff", b"\r\n", b""], {"Content-Type": "application/pdf"})
    client, session, _ = make([response])
    result = client.download_document(URL, max_bytes=4)
    assert result.content == b"\x00\xff\r\n" and result.content_type == "application/pdf"
    assert session.calls[0][2]["stream"] is True and response.closed


@pytest.mark.parametrize("headers,chunks,expected_chunks", [
    ({"Content-Length": "100"}, [b"unread"], 0), ({}, [b"123", b"45", b"unread"], 2),
    ({"Content-Length": "1"}, [b"12345", b"unread"], 1)])
def test_oversized_streams_stop_reading_close_and_do_not_retry(headers, chunks, expected_chunks):
    from finbot_ingestion.sec.errors import SECDocumentTooLarge
    response = StreamingResponse(chunks, headers)
    client, session, _ = make([response])
    with pytest.raises(SECDocumentTooLarge):
        client.download_document(URL, max_bytes=4)
    assert response.read_chunks == expected_chunks and response.closed
    assert len(session.calls) == 1


def test_bounded_stream_retry_retains_shared_dispatch_budget():
    first, second = StreamingResponse([b"partial"]), StreamingResponse([b"raw"])
    def fail(chunk_size):
        yield b"a"
        raise requests.ConnectionError("stream disconnected")
    first.iter_content = fail
    client, session, _ = make([first, second])
    assert client.download_document(URL, max_bytes=10).content == b"raw"
    assert len(session.calls) == 2 and first.closed and second.closed
    assert session.calls[1][0] - session.calls[0][0] >= 0.2
