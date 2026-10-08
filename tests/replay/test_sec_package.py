"""Synthetic SEC-shaped snapshots replayed through the real client and parsers."""
import json
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from finbot_ingestion.config import IngestionConfig
from finbot_ingestion.domain import Company, Filing
from finbot_ingestion.sec.client import SecClient
from finbot_ingestion.sec.errors import SECIncompletePackageError
from finbot_ingestion.sec.rate_limiter import SECRateLimiter
from finbot_ingestion.sec.urls import filing_index_url, accession_index_json_url, company_submissions_url

FIXTURES = Path(__file__).parents[1]/'fixtures'
TIME = datetime(2026,10,8,tzinfo=timezone.utc)

class Response:
    def __init__(self, content, status=200, headers=None):
        self.content, self.status_code, self.headers = content, status, headers or {}
    def close(self):
        pass

class Replay:
    def __init__(self, routes):
        self.routes, self.headers, self.starts = routes, {}, []
        self.time = 0.0
    def clock(self):
        return self.time
    def sleep(self, delay):
        self.time += delay
    def mount(self, *args):
        pass
    def get(self, url, **kwargs):
        self.starts.append(self.time)
        assert kwargs['allow_redirects'] is False
        return self.routes[url].popleft()
    def close(self):
        pass

def test_changed_package_replay_and_concurrent_request_classes():
    company = Company(ticker='AAPL',cik='320193',name='Apple')
    accession='0000320193-26-000001'
    target=Filing(accession_number=accession,company_cik=company.cik,ticker=company.ticker,form_type='8-K',filed_at=TIME,discovered_at=TIME,filing_index_url=filing_index_url(company.cik,accession),primary_document_name=None)
    index=(FIXTURES/'sec_package/index.html').read_bytes()
    directory=(FIXTURES/'sec_package/directory.json').read_bytes()
    incomplete=json.loads(directory)
    incomplete['directory']['item']=[{'name':'Primary.htm'}]
    submissions=(FIXTURES/'submissions_mixed.json').read_bytes()
    document='https://www.sec.gov/Archives/bytes.pdf'
    redirected='https://www.sec.gov/Archives/final.pdf'
    routes={target.filing_index_url:deque([Response(index)]*2),
            accession_index_json_url(company.cik,accession):deque([Response(json.dumps(incomplete).encode()),Response(directory)]),
            company_submissions_url(company.cik):deque([Response(submissions)]),
            document:deque([Response(b'',429),Response(b'',302,{'Location':redirected})]),
            redirected:deque([Response(b'\x00PDF\xff\r\n',headers={'Content-Type':'application/pdf'})])}
    replay=Replay(routes)
    limiter=SECRateLimiter(clock=replay.clock,sleeper=replay.sleep)
    with SecClient(IngestionConfig('Finbot owner@example.com'),limiter=limiter,session=replay,sleeper=replay.sleep,random_value=lambda:0) as client:
        with pytest.raises(SECIncompletePackageError):
            client.get_filing_index(target)
        with ThreadPoolExecutor(max_workers=3) as executor:
            filing_future=executor.submit(client.get_company_submissions,company)
            package_future=executor.submit(client.get_filing_index,target)
            download_future=executor.submit(client.download_document,document)
            assert len(filing_future.result())==6
            package=package_future.result()
            assert {d.filename for d in package.documents}=={'Primary.htm','release.htm','Slides.PDF','graphic.jpg'}
            assert package.primary_document_name=='Primary.htm'
            artifacts=package.artifacts(target,discovered_at=TIME)
            assert all(a.artifact_id==accession+'/'+a.filename for a in artifacts)
            assert download_future.result().content==b'\x00PDF\xff\r\n'
    assert len(replay.starts)==8
    assert all(sum(t<=x<=t+1 for x in replay.starts)<=5 for t in replay.starts)
