"""Synthetic source layouts only; never download the real financial pages."""

import pytest

from finbot_ingestion.sec.errors import SECDataError, SECIncompletePackageError
from finbot_ingestion.sec.filing_index import FilingIndex, PackageDocument
from finbot_ingestion.sec.urls import document_url
from finbot_ingestion.selection.images import supporting_images
from tests.support.dynamodb import make_filing


def select(html, names=("page.jpg", "logo.jpg", "chart.gif")):
    filing = make_filing()
    package = FilingIndex(tuple(PackageDocument(name, document_url(filing.company_cik,
        filing.accession_number, name), "GRAPHIC") for name in names), "Primary.htm")
    return supporting_images(filing, package, html.encode())


def test_pages_hidden_transcripts_and_smaller_financial_charts_survive():
    result = select('''<img src="logo.jpg" alt="Company logo" width="114" height="38">
        <img src="page.jpg" title="slide1" width="1365" height="769">
        <font style="font-size:1pt;color:white">Diluted EPS EUR 4.60</font>
        <p>Net sales by segment</p><img src="chart.gif" width="449" height="290">''')
    assert result["page.jpg"].selected and result["chart.gif"].selected
    assert result["logo.jpg"].reason == "decorative_branding"


def test_ambiguous_images_are_kept_and_content_use_wins():
    assert select('<img src="chart.gif">')["chart.gif"].selected
    result = select('<img src="page.jpg" alt="Logo" width="100" height="30">'
                    '<img src="page.jpg" width="1365" height="769">')
    assert result["page.jpg"].selected


def test_repeated_header_and_separator_are_excluded_without_fetching():
    result = select('<img src="page.jpg" width="163" height="23">' * 2
                    + '<img src="chart.gif" style="width:666px;height:1px">')
    assert result["page.jpg"].reason == "decorative_header"
    assert result["chart.gif"].reason == "decorative_separator"


@pytest.mark.parametrize("src", ["../page.jpg", "https://evil.test/page.jpg",
    "data:image/jpeg;base64,eA==", "page.jpg?x=1", "//evil.test/page.jpg", "sub/page.jpg", "https://[invalid"])
def test_unsafe_references_block_readiness(src):
    with pytest.raises(SECDataError, match="unsafe"):
        select(f'<img src="{src}">')


def test_missing_unsupported_and_excessive_assets_fail_visibly():
    with pytest.raises(SECIncompletePackageError):
        select('<img src="missing.jpg">')
    with pytest.raises(SECDataError, match="unsupported"):
        select('<img src="chart.svg">')
    names = tuple(f"page{i}.jpg" for i in range(129))
    with pytest.raises(SECDataError, match="count limit"):
        select(''.join(f'<img src="{name}">' for name in names), names)


def test_script_and_hidden_xbrl_images_are_not_dependencies():
    assert not select('<script><img src="bad.jpg"></script>'
                      '<ix:header><img src="bad.jpg"></ix:header>'
                      '<svg><img src="bad.jpg"></svg><template><img src="bad.jpg"></template>')


def test_malformed_dimensions_are_ambiguous_and_do_not_crash():
    assert select('<img src="page.jpg" style width>')["page.jpg"].selected


def test_hidden_transcriptions_do_not_suppress_the_coverage_diagnostic():
    from finbot_ingestion.selection.policy import image_only_html
    assert image_only_html(b'<img src="page.jpg"><font style="font-size:1pt;color:white">'
                           + b'financial table transcription ' * 100 + b'</font>')


@pytest.mark.parametrize('encoding', ['utf-16', 'utf-8-sig', 'cp1252'])
def test_image_references_respect_bom_and_declared_encoding(encoding):
    filing = make_filing()
    package = FilingIndex((PackageDocument('café.jpg', document_url(filing.company_cik,
        filing.accession_number, 'café.jpg'), 'GRAPHIC'),), 'Primary.htm')
    raw = f'<meta charset="{encoding}"><img src="café.jpg">'.encode(encoding)
    assert supporting_images(filing, package, raw)['café.jpg'].selected
