"""Earnings 6-K routing across explicit descriptions and primary-report layouts."""

from pathlib import Path

import pytest

from finbot_ingestion.sec.filing_index import FilingIndex, PackageDocument
from finbot_ingestion.sec.urls import document_url
from finbot_ingestion.selection.policy import MAX_ROUTING_PARSE_BYTES
from finbot_ingestion.selection.six_k import results_document, select_six_k
from tests.support.dynamodb import make_filing

FIXTURES = Path(__file__).parents[1] / "fixtures/sec_6k"


def package(rows, *, primary="Primary.htm"):
    filing = make_filing(form_type="6-K", primary_document_name=primary)
    documents = tuple(PackageDocument(name, document_url(filing.company_cik, filing.accession_number, name), kind, label)
                      for name, kind, label in rows)
    return filing, FilingIndex(documents, primary)


@pytest.mark.parametrize("label", [
    "Press release announcing financial results for Q2 2026",
    "Second-quarter earnings release 2026", "First quarter report 2026",
    "Consolidated Interim Financial Statements for the quarter ended June 30, 2026",
    "Statutory interim report for the six-month period ended June 28, 2026",
    "Full-year results 2026", "Annual financial report 2026",
    "Issuer reports net sales and net income in Q2 2026",
])
def test_affirmative_period_results_descriptions(label):
    assert results_document(label)


@pytest.mark.parametrize("label", [
    "EX-99.1", "Press release", "Financial statements", "AGM results 2026",
    "Will announce financial results for Q2 2026", "Date for Q2 2026 earnings release",
    "Board approved financial results for the quarter ended June 30, 2026",
    "See our Annual report 2026 for risk factors", "Q2 2026 revenue outlook",
    "Monthly net sales report for June 2026", "Q2 2026 earnings presentation",
    "Pro forma financial statements for the year ended December 31, 2026",
])
def test_generic_future_governance_and_historical_descriptions_do_not_admit(label):
    filing, listing = package([("Primary.htm", "6-K", "6-K"), ("release.htm", "EX-99.1", label)])
    assert not select_six_k(filing, listing)[0].selected


@pytest.mark.parametrize("fixture,selected", [
    ("asml_primary.html", {"Primary.htm", "release.htm", "statements.htm", "interim.pdf"}),
    ("wipro_primary.html", {"Primary.htm", "release.htm", "statements.htm", "interim.pdf", "metrics.htm"}),
])
def test_plain_exhibit_numbers_and_attached_item_paragraphs(fixture, selected):
    filing, listing = package([("Primary.htm", "6-K", "6-K"),
        ("release.htm", "EX-99.1", "EX-99.1"), ("deck.htm", "EX-99.2", "EX-99.2"),
        ("statements.htm", "EX-99.3", "EX-99.3"), ("interim.pdf", "EX-99.4", "EX-99.4"),
        ("metrics.htm", "EX-99.5", "EX-99.5"), ("logo.jpg", "GRAPHIC", "logo"),
        (make_filing().accession_number + ".txt", None, "Complete submission")])
    admission, decisions = select_six_k(filing, listing, (FIXTURES / fixture).read_bytes())
    assert admission.selected and admission.reason == "earnings_6_k"
    assert {name for name, decision in decisions.items() if decision.selected} == selected


def test_embedded_primary_report_qualifies_without_exhibits():
    filing, listing = package([("Primary.htm", "6-K", "6-K")])
    admission, decisions = select_six_k(filing, listing, (FIXTURES / "ericsson_primary.html").read_bytes())
    assert admission.selected and decisions["Primary.htm"].selected


def test_results_in_primary_prose_still_qualify_with_a_link_in_the_same_paragraph():
    filing, listing = package([("Primary.htm", "6-K", "6-K"),
        ("release.htm", "EX-99.1", "Earnings presentation")])
    content = b'<p>Issuer reports net sales and net income in Q2 2026. <a href="release.htm">Financial results release for Q2 2026</a></p>'
    admission, decisions = select_six_k(filing, listing, content)
    assert admission.selected and decisions["Primary.htm"].selected
    assert decisions["release.htm"].outcome == "UNRESOLVED"


@pytest.mark.parametrize("index_label,primary_description", [
    ("Financial results release for Q2 2026", '<p>99.1 Earnings presentation</p>'),
    ("Earnings presentation", '<p>99.1 Financial results release for Q2 2026</p>'),
    ("Financial results release for Q2 2026", '<p>99.1 Earnings press release and presentation for Q2 2026</p>'),
    ("Financial results release for Q2 2026", '<p>99.1 Transcript of Q2 2026 earnings call</p>'),
    ("Earnings presentation", '<p><a href="release.htm">Financial results release for Q2 2026</a></p>'),
])
def test_conflicting_only_results_document_cannot_admit_package(index_label, primary_description):
    filing, listing = package([("Primary.htm", "6-K", "6-K"),
        ("release.htm", "EX-99.1", index_label),
        ("statements.htm", "EX-99.2", "Consolidated financial statements")])
    admission, decisions = select_six_k(filing, listing, primary_description.encode())
    assert (admission.outcome, admission.reason) == ("UNRESOLVED", "unresolved_6_k")
    assert decisions == {}


@pytest.mark.parametrize("embedded_primary", [False, True])
def test_clear_results_document_admits_package_despite_conflicting_attachment(embedded_primary):
    rows = [("Primary.htm", "6-K", "6-K"),
        ("release.htm", "EX-99.1", "Financial results release for Q2 2026"),
        ("statements.htm", "EX-99.3", "Consolidated financial statements")]
    content = b'<p>99.1 Earnings presentation</p>'
    if embedded_primary:
        content += (FIXTURES / "ericsson_primary.html").read_bytes()
    else:
        rows.append(("report.pdf", "EX-99.2", "First quarter financial report 2026"))
    filing, listing = package(rows)
    admission, decisions = select_six_k(filing, listing, content)
    assert admission.selected and admission.reason == "earnings_6_k"
    assert decisions["Primary.htm"].selected
    assert decisions["release.htm"].outcome == "UNRESOLVED"
    assert decisions["statements.htm"].selected
    if not embedded_primary:
        assert decisions["report.pdf"].selected


@pytest.mark.parametrize("content", [
    b'<h1>Outcome of board meeting</h1><p>The Board approved financial results for the quarter ended June 30, 2026.</p>',
    b'<p>99.1 Issuer discloses 2026 AGM results, press release.</p>',
    b'<h1>Will announce quarterly financial results for Q2 2026</h1>',
])
def test_known_non_earnings_primary_is_excluded(content):
    filing, listing = package([("Primary.htm", "6-K", "6-K"), ("release.htm", "EX-99.1", "EX-99.1")])
    assert select_six_k(filing, listing, content)[0].outcome == "EXCLUDE"


@pytest.mark.parametrize("content", [None, b"Form 6-K", b"x" * (MAX_ROUTING_PARSE_BYTES + 1),
    b'<script>First quarter report 2026</script><!-- First quarter report 2026 --><p>6-K</p>',
    b'<p>Financial results</p><p>Q2 2026</p>',
])
def test_missing_opaque_oversized_or_cross_section_evidence_is_unresolved(content):
    filing, listing = package([("Primary.htm", "6-K", "6-K")])
    assert select_six_k(filing, listing, content)[0].outcome == "UNRESOLVED"


def test_ambiguous_numbers_do_not_assign_one_label_to_multiple_children():
    filing, listing = package([("Primary.htm", "6-K", "6-K"), ("a.htm", "EX-99.1", "EX-99.1"),
                              ("b.htm", "EX-99.1", "EX-99.1")])
    content = b'<h1>First quarter report 2026</h1><p>99.1 Earnings release for Q1 2026</p>'
    admission, decisions = select_six_k(filing, listing, content)
    assert admission.selected
    assert decisions["a.htm"].outcome == decisions["b.htm"].outcome == "UNRESOLVED"


def test_safe_links_bind_exact_files_and_external_links_cannot_bind_numbers():
    filing, listing = package([("Primary.htm", "6-K", "6-K"), ("release.htm", "EX-99.1", "EX-99.1")])
    content = b'<h1>First quarter report 2026</h1><p>99.1 <a href="https://example.com/release.htm">Earnings release for Q1 2026</a></p>'
    assert not select_six_k(filing, listing, content)[1]["release.htm"].selected
    content = content.replace(b'https://example.com/release.htm', b'release.htm')
    assert select_six_k(filing, listing, content)[1]["release.htm"].selected


def test_multi_exhibit_layout_and_combined_deck_remain_unresolved():
    filing, listing = package([("Primary.htm", "6-K", "6-K"), ("a.htm", "EX-99.1", "EX-99.1"),
                              ("b.htm", "EX-99.2", "EX-99.2")])
    content = b'<h1>First quarter report 2026</h1><p>Exhibit 99.1 and Exhibit 99.2: Earnings press release and presentation for Q1 2026</p>'
    _, decisions = select_six_k(filing, listing, content)
    assert all(not decisions[name].selected for name in ("a.htm", "b.htm"))


def test_pdf_release_can_qualify_from_index_without_parsing_pdf():
    filing, listing = package([("Primary.pdf", "6-K", "6-K"),
        ("release.pdf", "EX-99.1", "Financial results release for Q2 2026"),
        ("statements.pdf", "EX-99.2", "Consolidated financial statements")], primary="Primary.pdf")
    admission, decisions = select_six_k(filing, listing)
    assert admission.selected and all(d.selected for d in decisions.values())


@pytest.mark.parametrize('kind,name', [('EX-10.1', 'contract.htm'), ('EX-99.1', 'report.xml')])
def test_unrelated_or_unsupported_documents_cannot_admit_package(kind, name):
    filing, listing = package([('Primary.htm', '6-K', '6-K'), (name, kind, 'First quarter financial report 2026')])
    assert not select_six_k(filing, listing)[0].selected


def test_nonexistent_exhibit_description_cannot_admit_package():
    filing, listing = package([('Primary.htm', '6-K', '6-K')])
    assert not select_six_k(filing, listing, b'<p>99.1 Financial results release for Q2 2026</p>')[0].selected


@pytest.mark.parametrize('label', ['Issuer reports second-quarter 2026 results',
    '2026 first-quarter results', 'Issuer announces Q2 2026 results', 'First quarter results 2026'])
def test_common_reported_results_title_orders(label):
    assert results_document(label)
