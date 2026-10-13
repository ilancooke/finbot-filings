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
    primary_exhibits, primary_items, image_only_html, ExhibitEvidence, MAX_ROUTING_PARSE_BYTES, POLICY_VERSION)

CUT = datetime(2026, 10, 6, 4, tzinfo=timezone.utc)
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
    assert primary_exhibits(FILING, b'<table><tr><td><a href="https://evil.test/release.htm">Earnings release</a></td></tr></table>') == ExhibitEvidence({})


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


def test_dal_descriptive_link_outside_numbered_exhibit_table():
    parsed = package([('primary.htm', '8-K', 'Primary'),
        ('deltaairlinesannouncessept.htm', 'EX-99.1', 'EX-99.1')])
    content = b'''<div>Item 2.02 Results of Operations and Financial Condition</div>
    <div>The press release is furnished as Exhibit 99.1.</div>
    <div>Item 9.01 Financial Statements and Exhibits</div>
    <div><a href="deltaairlinesannouncessept.htm">Press Release dated October 9,
    titled Delta Air Lines Announces September Quarter Financial Results</a></div>'''
    refs = primary_exhibits(FILING, content)
    document = next(d for d in parsed.documents if d.filename != 'primary.htm')
    assert refs.item_2_02_numbers == {'99.1'}
    assert document_decision(FILING, parsed, document, references=refs) == Decision('SELECT', 'earnings_release')


def test_wfc_item_context_is_exact_and_does_not_bleed_into_next_section():
    parsed = package([('primary.htm', '8-K', 'Primary'),
        ('release.htm', 'EX-99.1', 'News Release dated October 13'),
        ('supplement.pdf', 'EX-99.2', '3Q26 Quarterly Supplement'),
        ('deck.htm', 'EX-99.3', 'Presentation'),
        ('other.htm', 'EX-99.10', 'News Release'), ('hyperlink.htm', 'EX-99.4', 'Hyperlinked PDF')])
    # Nested containers/headings, split spans and plural reference lists occur in SEC HTML.
    content = b'''<div><div><b>Item <span>2.02</span> Results of Operations</b></div>
      <p>The release and supplement are attached as Exhibits <span>99.1</span> and 99.2.</p>
      <p>Exhibit 99.4 is also furnished.</p>
      <div><strong>Item 7.01 Regulation FD Disclosure</strong></div>
      <p>Financial results presentation Exhibit 99.3 and news release Exhibit 99.10.</p>
      <div>Item 9.01 Financial Statements and Exhibits</div></div>'''
    refs = primary_exhibits(FILING, content)
    assert refs.item_2_02_numbers == {'99.1', '99.2', '99.4'}
    results = {d.filename: document_decision(FILING, parsed, d, references=refs) for d in parsed.documents}
    assert results['release.htm'] == Decision('SELECT', 'earnings_release')
    assert results['supplement.pdf'] == Decision('SELECT', 'financial_supplement')
    assert results['deck.htm'] == Decision('EXCLUDE', 'presentation')
    assert results['other.htm'].outcome == results['hyperlink.htm'].outcome == 'UNRESOLVED'
    assert not document_decision(FILING, parsed, next(d for d in parsed.documents if d.filename == 'supplement.pdf')).selected


@pytest.mark.parametrize('description', [
    'Unaudited Comparative Supplementary Sales Data and Condensed Consolidated Statement of Earnings',
    'Supplemental sales data', 'Financial statements', 'Condensed statement of income',
    'Consolidated statement of operations', 'STATEMENT\nOF\tEARNINGS',
])
def test_explicit_results_statements_need_no_item_reference(description):
    parsed = package([('primary.htm', '8-K', 'Primary'), ('supplement.htm', 'EX-99.2', description)])
    document = next(d for d in parsed.documents if d.filename == 'supplement.htm')
    assert document_decision(FILING, parsed, document) == Decision('SELECT', 'financial_supplement')


def test_jnj_supplement_label_in_split_links_without_item_2_02_reference():
    parsed = package([('primary.htm', '8-K', 'Primary'), ('supplement.htm', 'EX-99.2', 'EX-99.2')])
    content = b'''<div>Item 2.02 Results of Operations</div><p>Press release Exhibit 99.1.</p>
    <div>Item 9.01 Financial Statements and Exhibits</div>
    <table><tr><td><a href="supplement.htm">99.2</a></td><td>
    <a href="supplement.htm">Unaudited Comparative Supplementary Sales Data and</a>
    <a href="supplement.htm">Condensed Consolidated Statement of Earnings</a></td></tr></table>'''
    refs = primary_exhibits(FILING, content)
    assert refs.item_2_02_numbers == {'99.1'}
    assert document_decision(FILING, parsed, next(d for d in parsed.documents if d.filename == 'supplement.htm'),
        references=refs).reason == 'financial_supplement'


@pytest.mark.parametrize('description', ['News release', 'Press release', 'Quarterly supplement'])
@pytest.mark.parametrize('section', ['7.01', '9.01'])
def test_generic_roles_in_other_sections_do_not_authorize_downloads(description, section):
    parsed = package([('primary.htm', '8-K', 'Primary'), ('other.htm', 'EX-99.2', description)])
    content = f'''<p>Item 2.02 Results of Operations</p><p>See Item {section} for Exhibit 99.1.</p>
    <p>Item {section} Other Disclosure</p><p>Financial results release attached as Exhibit 99.2.</p>'''.encode()
    refs = primary_exhibits(FILING, content)
    assert refs.item_2_02_numbers == {'99.1'}
    assert not document_decision(FILING, parsed, next(d for d in parsed.documents if d.filename == 'other.htm'),
        references=refs).selected


def test_exact_number_required_and_plain_text_item_boundaries():
    parsed = package([('primary.htm', '8-K', 'Primary'), ('other.htm', 'EX-99.1', 'News release')])
    refs = primary_exhibits(FILING, b'Item 2.02 Results of Operations\nExhibit 99.10.\nItem 7.01 Other\nExhibit 99.1.')
    assert refs.item_2_02_numbers == {'99.10'}
    assert not document_decision(FILING, parsed, next(d for d in parsed.documents if d.filename == 'other.htm'),
        references=refs).selected


def test_comments_scripts_and_styles_are_not_exhibit_references():
    refs = primary_exhibits(FILING, b'''<p>Item 2.02 Results</p><p>Exhibit 99.1.</p>
    <!-- Exhibit 99.2 --><script>Exhibit 99.3</script><style>Exhibit 99.4</style>
    <p>Please refer to Item 7.01 for more information.</p><p>Exhibit 99.5.</p>
    <p>Item 9.01 Exhibits</p><p>Exhibit 99.6.</p>''')
    assert refs.item_2_02_numbers == {'99.1', '99.5'}


@pytest.mark.parametrize('description', ['Hyperlinked PDF', 'Securities registered pursuant to Section 12',
                                        'Sales data', 'Quarterly results', 'Remarks', 'EX-99.1'])
def test_generic_or_unrelated_labels_remain_unresolved_even_in_results_item(description):
    parsed = package([('primary.htm', '8-K', 'Primary'), ('other.htm', 'EX-99.1', description)])
    refs = primary_exhibits(FILING, b'<p>Item 2.02 Results</p><p>Exhibit 99.1.</p>')
    assert document_decision(FILING, parsed, next(d for d in parsed.documents if d.filename == 'other.htm'),
        references=refs) == Decision('UNRESOLVED', 'unresolved_exhibit_role')


def test_multi_attachment_row_and_paragraph_do_not_share_roles():
    parsed = package([('primary.htm', '8-K', 'Primary'), ('release.htm', 'EX-99.1', 'EX-99.1'),
        ('other.htm', 'EX-99.2', 'EX-99.2')])
    content = b'''<p>Financial statements: <a href="release.htm">Earnings release</a>
    and <a href="other.htm">99.2</a></p>
    <table><tr><td>99.1</td><td><a href="release.htm">Earnings release</a>
    and <a href="other.htm">99.2</a></td></tr></table>'''
    refs = primary_exhibits(FILING, content)
    results = {d.filename: document_decision(FILING, parsed, d, references=refs) for d in parsed.documents}
    assert results['release.htm'].selected
    assert results['other.htm'].outcome == 'UNRESOLVED'


@pytest.mark.parametrize('description', ['Financial statements presentation', 'Quarterly supplement and slides',
                                       'News release and presentation'])
def test_new_financial_roles_do_not_override_presentation_conflicts(description):
    parsed = package([('primary.htm', '8-K', 'Primary'), ('mixed.htm', 'EX-99.1', description)])
    refs = primary_exhibits(FILING, b'<p>Item 2.02 Results</p><p>Exhibit 99.1.</p>')
    assert document_decision(FILING, parsed, next(d for d in parsed.documents if d.filename == 'mixed.htm'),
        references=refs).outcome == 'UNRESOLVED'


def test_external_or_other_accession_link_and_oversized_primary_supply_no_role():
    content = b'''<p><a href="https://evil.test/release.htm">Earnings release</a>
    <a href="../another/release.htm">Financial statements</a></p>'''
    assert primary_exhibits(FILING, content) == ExhibitEvidence({})
    assert primary_exhibits(FILING, b'x' * (MAX_ROUTING_PARSE_BYTES + 1)) == ExhibitEvidence({})
