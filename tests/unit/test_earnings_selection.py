"""Synthetic SEC-shaped examples; no provider calls or downloaded large filings."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from finbot_ingestion.config import ConfigurationError
from finbot_ingestion.domain import Filing
from finbot_ingestion.domain.checkpoints import ArtifactCheckpoint
from finbot_ingestion.sec.filing_index import FilingIndex, PackageDocument, parse_filing_index
from finbot_ingestion.sec.urls import document_url, filing_index_url
from finbot_ingestion.selection.policy import (AcquisitionPolicy, Decision, document_decision,
    primary_exhibits, primary_items, image_only_html, POLICY_VERSION)

CUT = datetime(2026, 10, 10, tzinfo=timezone.utc)
ACC = "0001418135-26-000048"
FILING = Filing(accession_number=ACC, company_cik="1418135", ticker="KDP", form_type="8-K",
    filed_at=CUT, discovered_at=CUT, primary_document_name="primary.htm",
    filing_index_url=filing_index_url("1418135", ACC))


def package(rows, *, filing=FILING, items="Item 2.02: Results of Operations; Item 9.01: Exhibits"):
    # Minimal source-shaped HTML/directory snapshots, intentionally synthetic.
    body = ('<div class="infoHead">Items</div><div class="info">' + items + '</div>'
            '<table><tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th></tr>')
    body += ''.join(f'<tr><td>{i}</td><td>{desc}</td><td><a href="{name}">{name}</a></td><td>{kind}</td></tr>'
                    for i, (name, kind, desc) in enumerate(rows, 1))
    return parse_filing_index(filing, (body + '</table>').encode(),
        {'directory': {'item': [{'name': name} for name, _, _ in rows]}})


@pytest.mark.parametrize('form', ['10-K', '10-K/A', '10-Q', '10-Q/A'])
def test_cutoff_is_inclusive_and_periodic_forms_need_no_item_metadata(form):
    policy = AcquisitionPolicy.from_env({})
    filing = replace(FILING, form_type=form)
    assert policy.live_start_at == CUT
    assert policy.filing(filing).selected
    assert policy.filing(replace(filing, filed_at=CUT - timedelta(microseconds=1))) == Decision('EXCLUDE', 'before_live_cutoff')
    assert AcquisitionPolicy.from_env({}).live_start_at == policy.live_start_at


@pytest.mark.parametrize('form', ['8-K', '8-K/A'])
def test_earnings_item_is_exact_and_unknown_is_visible(form):
    policy = AcquisitionPolicy(CUT)
    filing = replace(FILING, form_type=form)
    assert policy.filing(filing, items=('2.02', '9.01'), item_status='known').selected
    assert policy.filing(filing, items=('8.01', '9.01'), item_status='known').reason == 'non_earnings_8_k'
    assert policy.filing(filing, items=('2.020',), item_status='known').outcome == 'EXCLUDE'
    assert policy.filing(filing, item_status='ambiguous').outcome == 'UNRESOLVED'
    assert policy.filing(filing).outcome == 'UNRESOLVED'


@pytest.mark.parametrize('value', ['', '2026-10-10', '2026-10-10T00:00:00', 'garbage'])
def test_cutoff_requires_explicit_timezone(value):
    with pytest.raises(ConfigurationError, match='LIVE_INGESTION_START_AT'):
        AcquisitionPolicy.from_env({'LIVE_INGESTION_START_AT': value})


def test_index_preserves_descriptions_and_structural_item_evidence():
    parsed = package([('primary.htm', '8-K', 'Primary'), ('release.htm', 'EX-99.1', 'Earnings release')])
    assert parsed.items == ('2.02', '9.01') and parsed.item_status == 'known'
    assert next(d for d in parsed.documents if d.filename == 'release.htm').description == 'Earnings release'
    assert package([('primary.htm', '8-K', 'Primary')], items='unavailable').item_status == 'ambiguous'


def test_kdp_release_kept_without_standalone_logos_or_complete_text():
    parsed = package([('primary.htm', '8-K', 'Primary'), ('release.htm', 'EX-99.1', 'Earnings release'),
                      ('logo.jpg', 'GRAPHIC', 'Logo'), (ACC + '.txt', '', 'Complete submission text')])
    results = {d.filename: document_decision(FILING, parsed, d) for d in parsed.documents}
    assert {name for name, result in results.items() if result.selected} == {'primary.htm', 'release.htm'}
    assert results['logo.jpg'].reason == 'standalone_graphic'
    assert results[ACC + '.txt'].reason == 'submission_bundle'


def test_cna_supplement_not_assumed_to_be_deck_and_other_number_release_allowed():
    parsed = package([('primary.htm', '8-K', 'Primary'), ('release.htm', 'EX-99.1', 'Earnings release'),
        ('supplement.pdf', 'EX-99.2', 'Financial supplement'), ('deck.htm', 'EX-99.3', 'Earnings results presentation'),
        ('release.txt', 'EX-13.1', 'Financial results press release'), ('data.xml', 'XML', 'Auxiliary')])
    results = {d.filename: document_decision(FILING, parsed, d) for d in parsed.documents}
    assert results['supplement.pdf'].reason == 'financial_supplement'
    assert results['deck.htm'] == Decision('EXCLUDE', 'presentation')
    assert results['release.txt'].selected
    assert results['data.xml'].reason == 'auxiliary_file'


def test_periodic_report_keeps_only_primary():
    filing = replace(FILING, form_type='10-Q/A')
    parsed = package([('primary.htm', '10-Q/A', 'Report'), ('release.htm', 'EX-99.1', 'Earnings release')], filing=filing)
    assert [d.filename for d in parsed.documents if document_decision(filing, parsed, d).selected] == ['primary.htm']


def test_generic_exhibit_resolved_by_primary_release_reference():
    parsed = package([('primary.htm', '8-K', 'Primary'), ('release.htm', 'EX-99.2', 'EX-99.2')])
    document = next(d for d in parsed.documents if d.filename == 'release.htm')
    assert document_decision(FILING, parsed, document).outcome == 'UNRESOLVED'
    content = b'''<p><b>Item 2.02 Results of Operations and Financial Condition</b></p>
    <p>Financial results were announced in a press release attached as Exhibit 99.2.</p>
    <table><tr><td>99.2</td><td><a href="release.htm">Press Release dated October 10</a></td></tr></table>'''
    assert primary_items(content) == ('2.02',)
    assert primary_items(b'Item 2.02 Results of Operations\n\nItem 9.01 Exhibits') == ('2.02', '9.01')
    refs = primary_exhibits(FILING, content)
    assert document_decision(FILING, parsed, document, references=refs).selected
    assert not primary_items(b'<p>Please refer to Item 2.02 in another document.</p>')
    assert primary_exhibits(FILING, b'<table><tr><td><a href="https://evil.test/release.htm">Earnings release</a></td></tr></table>') == {}


def test_combined_or_conflicting_roles_are_unresolved():
    parsed = package([('primary.htm', '8-K', 'Primary'), ('mixed.pdf', 'EX-99.1', 'Earnings release and presentation')])
    assert document_decision(FILING, parsed, parsed.documents[0]).outcome == 'UNRESOLVED'  # sorted: mixed first


def test_image_only_diagnostic_does_not_fetch_or_extract():
    assert image_only_html(b'<html><body>Financial results<img src="huge.jpg"></body></html>')
    assert not image_only_html(b'<html><body>' + b'revenue 100 dollars ' * 20 + b'<img src="logo.jpg"></body></html>')


def test_selection_checkpoint_requires_paired_fields_and_valid_reason():
    identity = ACC + '/release.htm'
    with pytest.raises(ValueError, match='selection checkpoint'):
        ArtifactCheckpoint(artifact_id=identity, selection_policy=POLICY_VERSION)
    checkpoint = ArtifactCheckpoint(artifact_id=identity, selection_policy=POLICY_VERSION,
        selection_decision='UNRESOLVED', selection_reason='unresolved_exhibit_role', selection_checked_at=CUT)
    assert checkpoint.selection_blocked and not checkpoint.pending_dead_letter


def test_nested_layout_tables_do_not_mix_exhibit_roles_or_select_remarks():
    parsed = package([('primary.htm', '8-K', 'Primary'), ('release.htm', 'EX-99.1', 'EX-99.1'),
        ('supplement.pdf', 'EX-99.2', 'EX-99.2'), ('deck.htm', 'EX-99.3', 'EX-99.3'),
        ('remarks.htm', 'EX-99.4', 'EX-99.4')])
    content = b'''<table><tr><td><table>
      <tr><td><a href="release.htm">99.1</a></td><td>Press release about quarterly financial results</td></tr>
      <tr><td><a href="supplement.pdf">99.2</a></td><td>Financial supplement</td></tr>
      <tr><td><a href="deck.htm">99.3</a></td><td>Earnings presentation with quarterly results</td></tr>
      <tr><td><a href="remarks.htm">99.4</a></td><td>Earnings remarks about quarterly results</td></tr>
    </table></td></tr></table>'''
    refs = primary_exhibits(FILING, content)
    results = {d.filename: document_decision(FILING, parsed, d, references=refs) for d in parsed.documents}
    assert results['release.htm'].selected and results['supplement.pdf'].selected
    assert results['deck.htm'].reason == 'presentation'
    assert not results['remarks.htm'].selected


def test_inline_results_need_no_exhibit():
    parsed = package([('primary.htm', '8-K', 'Report with inline financial results')])
    assert [d.filename for d in parsed.documents if document_decision(FILING, parsed, d).selected] == ['primary.htm']
