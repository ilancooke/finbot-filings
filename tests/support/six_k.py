"""Public SEC captures keyed by the selection behavior they exercise."""

from dataclasses import dataclass
import json
from pathlib import Path

from finbot_ingestion.domain import Filing
from finbot_ingestion.sec.client import DownloadedDocument
from finbot_ingestion.sec.filing_index import FilingIndex, PackageDocument
from finbot_ingestion.sec.urls import filing_index_url
from tests.support.dynamodb import NOW

FIXTURES = Path(__file__).parents[1] / 'fixtures/sec_6k/captured_layouts'


@dataclass(frozen=True)
class Capture:
    ticker: str
    cik: str
    accession: str
    primary: str
    release: str
    release_fixture: str | None = None


CASES = {
    'wrapped_description': Capture('ERIC', '0000717826', '0001628280-26-065962',
        'a2610156-kq326.htm', 'a2026q3forfiling.htm'),
    'generic_release': Capture('TSM', '0001046179', '0001046179-26-000726',
        'tsm-20261015x6k.htm', 'a3q26e_withguidancexfinal.htm', 'generic_release_attachment.html'),
}


def capture_content(layout, filename):
    case = CASES[layout]
    if filename == case.primary:
        return (FIXTURES / f'{layout}_primary.html').read_bytes()
    assert filename == case.release
    return ((FIXTURES / case.release_fixture).read_bytes() if case.release_fixture
            else b'original quarterly report bytes')


def captured(layout):
    case = CASES[layout]
    filing = Filing(accession_number=case.accession, company_cik=case.cik, ticker=case.ticker,
        form_type='6-K', filed_at=NOW, discovered_at=NOW, company_name=case.ticker + ' company',
        filing_index_url=filing_index_url(case.cik, case.accession), primary_document_name=case.primary)
    package = FilingIndex(tuple(PackageDocument(**row) for row in
        json.loads((FIXTURES / f'{layout}_package.json').read_text())), case.primary)
    return filing, package, case.release


def wire_captures(system, monkeypatch):
    filings, packages, downloads = {}, {}, []
    for layout in CASES:
        filing, package, _ = captured(layout)
        filings[filing.accession_number] = filing
        packages[filing.accession_number] = package
    monkeypatch.setattr(system.sec, 'get_filing_index', lambda filing: packages[filing.accession_number])
    def download(url, *, max_bytes, context=None):
        name = url.rsplit('/', 1)[-1]
        # The generic release's ambiguous small raster is retained conservatively.
        # Presentation assets and other unreferenced graphics still fail here.
        if name == 'image_1.jpg' and CASES['generic_release'].accession.replace('-', '') in url:
            downloads.append((name, max_bytes))
            return DownloadedDocument(b'\xff\xd8\xffSynthetic raster', 'image/jpeg', url)
        layout = next((layout for layout, case in CASES.items() if name in (case.primary, case.release)), None)
        assert layout is not None
        downloads.append((name, max_bytes))
        body = capture_content(layout, name)
        assert len(body) <= max_bytes
        return DownloadedDocument(body, 'text/html', url)
    monkeypatch.setattr(system.sec, 'download_document', download)
    return filings, packages, downloads
