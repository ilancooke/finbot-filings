from __future__ import annotations

from pathlib import Path

import pytest

from finbot_filings.parsing.models import FailureReason, ParseStatus
from finbot_filings.parsing.toc import parse_filing_sections

FIXTURES = Path(__file__).parent / "fixtures"


def parse_fixture(name: str, form_type: str = "10-Q"):
    path = FIXTURES / name
    return parse_filing_sections(path.read_bytes(), file=path, form_type=form_type)


def test_normal_10q_distinguishes_parts_and_ignores_non_section_links() -> None:
    result = parse_fixture("normal_10q.html")
    assert result.status is ParseStatus.SUCCESS
    assert result.sections_found == 11
    section_ids = {section.section_id for section in result.sections}
    assert "part1_item1_financial_statements" in section_ids
    assert "part2_item1_legal_proceedings" in section_ids
    assert all(section.anchor_id != "footnote" for section in result.sections)
    assert result.diagnostics.native_outline_entries_detected == 11
    assert result.diagnostics.native_outline_entries_extracted == 11
    assert result.diagnostics.native_outline_coverage == 1.0


def test_text_is_readable_and_hidden_metadata_is_removed() -> None:
    result = parse_fixture("normal_10q.html")
    financials = next(
        section
        for section in result.sections
        if section.section_id == "part1_item1_financial_statements"
    )
    assert "Financial statements & accompanying notes." in financials.text
    assert "HIDDEN IXBRL VALUE" not in financials.text


def test_physical_dom_order_controls_boundaries_with_opaque_ids() -> None:
    result = parse_fixture("physical_order_10q.html")
    assert result.status is ParseStatus.SUCCESS
    assert result.sections[0].section_id == "part1_item2_mda"
    assert result.sections[1].section_id == "part1_item1_financial_statements"
    assert "MD&A physically comes first" in result.sections[0].text
    assert "Financial statements physically come second" not in result.sections[0].text


def test_missing_anchor_returns_partial_native_output() -> None:
    result = parse_fixture("missing_anchor_10q.html")
    assert result.status is ParseStatus.PARTIAL
    assert result.failure_reason is None
    assert "missing-i4" in result.diagnostics.unresolved_anchor_ids
    assert "part1_item4_controls" in result.diagnostics.unresolved_section_ids
    assert result.diagnostics.native_outline_entries_detected == 7
    assert result.diagnostics.native_outline_entries_extracted == 6
    assert result.diagnostics.native_outline_entries_skipped == [
        {
            "toc_text": "Item 4. Controls and Procedures",
            "reason": "missing_anchor_target",
        }
    ]


def test_10q_preserves_noncanonical_native_items_under_unlinked_part_rows() -> None:
    html = (FIXTURES / "normal_10q.html").read_text(encoding="utf-8")
    html = html.replace(
        '<tr><td><a href="#part-one">Part I — Financial Information</a></td></tr>',
        '<tr><td>Part I — Financial Information</td></tr>',
    ).replace(
        '<tr><td>Item 1. <a href="#financial-statements">Financial Statements</a></td></tr>',
        """<tr><td>Item 1A. <a href="#group-financials">Condensed Consolidated Financial Statements of Parent Holdings Inc.</a></td></tr>
        <tr><td><a href="#group-balance-sheets">Condensed Consolidated Balance Sheets</a></td></tr>
        <tr><td>Item 1B. <a href="#subsidiary-financials">Condensed Consolidated Financial Statements of Operating Subsidiary, Inc.</a></td></tr>""",
    ).replace(
        '<h2 id="financial-statements">Item 1. Financial Statements</h2>',
        """<h2 id="group-financials">Item 1A. Condensed Consolidated Financial Statements of Parent Holdings Inc.</h2>
        <h3 id="group-balance-sheets">Condensed Consolidated Balance Sheets</h3>
        <h2 id="subsidiary-financials">Item 1B. Condensed Consolidated Financial Statements of Operating Subsidiary, Inc.</h2>""",
    )

    result = parse_filing_sections(
        html.encode(), file=Path("combined-registrant-10q.html"), form_type="10-Q"
    )

    assert result.status is ParseStatus.SUCCESS
    assert result.sections_found == 12
    assert result.canonical_sections_mapped == 10
    assert result.semantic_only_sections == 2
    assert result.mapping_status.value == "partial"
    assert result.diagnostics.native_outline_entries_detected == 12
    assert result.diagnostics.native_outline_entries_extracted == 12
    assert all(
        section.source_title != "Condensed Consolidated Balance Sheets"
        for section in result.sections
    )
    financials = {
        section.source_section_id: section
        for section in result.sections
        if section.source_section_id in {"part1_item1a", "part1_item1b"}
    }
    assert set(financials) == {"part1_item1a", "part1_item1b"}
    assert financials["part1_item1a"].canonical_section_id is None
    assert financials["part1_item1a"].semantic_categories == (
        "financial_statements",
    )
    assert financials["part1_item1a"].registrant_name == "Parent Holdings Inc"
    assert financials["part1_item1b"].registrant_name == "Operating Subsidiary, Inc"
    assert next(
        section
        for section in result.sections
        if section.source_section_id == "part2_item1a"
    ).canonical_section_id == "part2_item1a_risk_factors"


def test_topic_oriented_10k_preserves_native_outline_without_item_mapping() -> None:
    topics = [
        ("Overview", "overview"),
        ("Our Strategy", "strategy"),
        ("Our Business", "business"),
        ("Operating Segment Results", "segments"),
        ("Risk Factors", "risks"),
        ("Liquidity and Capital Resources", "liquidity"),
        ("Consolidated Financial Statements", "financials"),
        ("Controls and Procedures", "controls"),
    ]
    rows = "".join(
        f"<tr><td><a href='#{anchor}'>{title}</a></td><td>{number}</td></tr>"
        for number, (title, anchor) in enumerate(topics, start=1)
    )
    # Some SEC anchors sit immediately after a visible heading, so the target's
    # following text need not repeat the TOC caption.
    targets = "".join(
        f"<h2>{title}</h2><div id='{anchor}'></div><p>{title} body.</p>"
        for title, anchor in topics
    )
    html = f"<html><body><h1>Table of Contents</h1><table>{rows}</table>{targets}</body></html>"

    result = parse_filing_sections(
        html.encode(), file=Path("topic-outline-10k.html"), form_type="10-K"
    )

    assert result.status is ParseStatus.SUCCESS
    assert result.sections_found == 8
    assert result.canonical_sections_mapped == 0
    assert result.sections[0].source_title == "Overview"
    assert all(section.item is None for section in result.sections)


def test_topic_outline_keeps_separate_links_that_share_a_table_row() -> None:
    topics = [
        ("Overview", "overview"),
        ("Strategy", "strategy"),
        ("Business", "business"),
        ("Segments", "segments"),
        ("Risk Factors", "risks"),
        ("Liquidity", "liquidity"),
        ("Financial Statements", "financials"),
        ("Controls", "controls"),
    ]
    first_row = (
        "<tr><td><a href='#overview'>Overview</a></td>"
        "<td><a href='#strategy'>Strategy</a></td></tr>"
    )
    rows = first_row + "".join(
        f"<tr><td><a href='#{anchor}'>{title}</a></td></tr>"
        for title, anchor in topics[2:]
    )
    targets = "".join(
        f"<h2 id='{anchor}'>{title}</h2><p>{title} body.</p>"
        for title, anchor in topics
    )
    html = f"<h1>Table of Contents</h1><table>{rows}</table>{targets}"

    result = parse_filing_sections(
        html.encode(), file=Path("two-column-outline.html"), form_type="10-K"
    )

    assert result.status is ParseStatus.SUCCESS
    assert [section.source_title for section in result.sections[:2]] == [
        "Overview",
        "Strategy",
    ]


def test_no_recognizable_toc_fails() -> None:
    html = b"<html><body><p>Table of Contents</p><a href='#note'>Note 1</a><div id='note'>Text</div></body></html>"
    result = parse_filing_sections(html, file=Path("unknown.html"), form_type="10-Q")
    assert result.status is ParseStatus.FAILURE
    assert result.failure_reason is FailureReason.NO_RECOGNIZED_ITEM_LINKS


def test_no_internal_links_fails_as_no_toc() -> None:
    result = parse_filing_sections(
        b"<html><body><h2>Item 1</h2></body></html>",
        file=Path("headings-only.html"),
        form_type="10-Q",
    )
    assert result.failure_reason is FailureReason.NO_TOC_FOUND


def test_item_links_without_part_assignment_fail_instead_of_guessing() -> None:
    html = b"<html><body><p>Table of Contents</p><a href='#one'>Item 1</a><h2 id='one'>Item 1</h2></body></html>"
    result = parse_filing_sections(html, file=Path("ambiguous.html"), form_type="10-Q")
    assert result.failure_reason is FailureReason.AMBIGUOUS_PART_ASSIGNMENT


def test_10q_can_assign_parts_from_unique_canonical_toc_captions() -> None:
    html = b"""
    <html><body><p>Table of Contents</p><table>
      <tr><td><a href='#i1'>Item 1. Financial Statements</a></td></tr>
      <tr><td><a href='#i2'>Item 2. Management's Discussion and Analysis of Financial Condition and Results of Operations</a></td></tr>
      <tr><td><a href='#i4'>Item 4. Controls and Procedures</a></td></tr>
      <tr><td><a href='#ii1'>Item 1. Legal Proceedings</a></td></tr>
      <tr><td><a href='#ii1a'>Item 1A. Risk Factors</a></td></tr>
      <tr><td><a href='#ii6'>Item 6. Exhibits</a></td></tr>
    </table>
    <h2 id='i1'>Financials</h2><p>One</p>
    <h2 id='i2'>MD&amp;A</h2><p>Two</p>
    <h2 id='i4'>Controls</h2><p>Four</p>
    <h2 id='ii1'>Legal</h2><p>Legal</p>
    <h2 id='ii1a'>Risks</h2><p>Risks</p>
    <h2 id='ii6'>Exhibits</h2><p>Exhibits</p>
    </body></html>
    """
    result = parse_filing_sections(
        html, file=Path("caption-parts.html"), form_type="10-Q"
    )
    assert result.status is ParseStatus.SUCCESS
    assert result.sections_found == 6
    assert len(result.diagnostics.caption_based_part_assignments) == 6
    assert {section.part for section in result.sections} == {"part1", "part2"}


def test_normal_10k_is_supported() -> None:
    result = parse_fixture("normal_10k.html", "10-K")
    assert result.status is ParseStatus.SUCCESS
    assert result.sections_found == 10
    assert result.diagnostics.itemless_toc_fallback_used is False
    assert result.mapping_status.value == "complete"
    assert {section.section_id for section in result.sections}.issuperset(
        {"item1_business", "item1a_risk_factors", "item7_mda", "item8_financial_statements"}
    )


def test_10k_prefers_item_and_title_links_over_conflicting_page_number_link() -> None:
    html = (FIXTURES / "normal_10k.html").read_text(encoding="utf-8")
    html = html.replace(
        '<tr><td><a href="#properties">Item 2. Properties</a></td></tr>',
        """<tr>
          <td><a href="#cybersecurity">Item 1C.</a></td>
          <td><a href="#cybersecurity">Cybersecurity</a></td>
          <td><a href="#risks">42</a></td>
        </tr>
        <tr><td><a href="#properties">Item 2. Properties</a></td></tr>""",
    ).replace(
        '<h2 id="properties">Item 2</h2>',
        '<h2 id="cybersecurity">Item 1C</h2><p>Cybersecurity text.</p>\n'
        '<h2 id="properties">Item 2</h2>',
    )

    result = parse_filing_sections(
        html.encode(), file=Path("amd-style-10k.html"), form_type="10-K"
    )

    assert result.status is ParseStatus.SUCCESS
    cybersecurity = next(
        entry
        for entry in result.recognized_toc_entries
        if entry.section_id == "item1c_cybersecurity"
    )
    assert cybersecurity.anchor_id == "cybersecurity"
    assert result.diagnostics.ambiguous_item_classifications == []


def test_10k_preserves_noncanonical_multi_registrant_financial_sections() -> None:
    html = (FIXTURES / "normal_10k.html").read_text(encoding="utf-8")
    html = html.replace(
        '<tr><td><a href="#financials">Item 8. Financial Statements</a></td></tr>',
        """<tr><td><a href="#group-financials">Item 8A. Consolidated Financial Statements and Supplementary Data of Parent Holdings Inc.</a></td></tr>
        <tr><td><a href="#subsidiary-financials">Item 8B. Consolidated Financial Statements and Supplementary Data of Operating Subsidiary, Inc.</a></td></tr>""",
    ).replace(
        '<h2 id="financials">Item 8</h2><p>Financial text.</p>',
        """<h2 id="group-financials">Item 8A. Consolidated Financial Statements and Supplementary Data of Parent Holdings Inc.</h2><p>Parent financial text.</p>
        <h2 id="subsidiary-financials">Item 8B. Consolidated Financial Statements and Supplementary Data of Operating Subsidiary, Inc.</h2><p>Subsidiary financial text.</p>""",
    )

    result = parse_filing_sections(
        html.encode(), file=Path("combined-registrant-10k.html"), form_type="10-K"
    )

    assert result.status is ParseStatus.SUCCESS
    assert result.mapping_status.value == "partial"
    financials = {section.item: section for section in result.sections if section.item in {"8A", "8B"}}
    assert set(financials) == {"8A", "8B"}
    assert financials["8A"].canonical_section_id is None
    assert financials["8A"].semantic_categories == ("financial_statements",)
    assert financials["8A"].registrant_name == "Parent Holdings Inc"
    assert financials["8B"].registrant_name == "Operating Subsidiary, Inc"


def test_10k_still_uses_numeric_page_link_when_it_is_the_only_row_link() -> None:
    html = (FIXTURES / "normal_10k.html").read_text(encoding="utf-8").replace(
        '<tr><td><a href="#business">Item 1. Business</a></td></tr>',
        '<tr><td>Item 1. Business</td><td><a href="#business">1</a></td></tr>',
    )

    result = parse_filing_sections(
        html.encode(), file=Path("page-only-10k.html"), form_type="10-K"
    )

    assert result.status is ParseStatus.SUCCESS
    business = next(
        entry
        for entry in result.recognized_toc_entries
        if entry.section_id == "item1_business"
    )
    assert business.anchor_id == "business"


def test_10k_ignores_item_cross_references_outside_the_toc_region() -> None:
    html = (FIXTURES / "normal_10k.html").read_text(encoding="utf-8")
    html = html.replace(
        '<h2 id="properties">Item 2</h2>',
        """<p>
          See <a href="#legal">Item 3. Legal Proceedings</a> and
          <a href="#note-10">Note 10</a> in this report.
        </p>
        <h2 id="properties">Item 2</h2>""",
    ).replace(
        '<h2 id="financials">Item 8</h2>',
        """<p>
          Legal proceedings are discussed in <a href="#note-10">Note 10</a>
          included in <a href="#financials">Item 8. Financial Statements</a>.
        </p>
        <h2 id="financials">Item 8</h2>""",
    ).replace(
        "</body>",
        '<h2 id="note-10">Note 10. Contingencies</h2><p>Note text.</p>\n</body>',
    )

    result = parse_filing_sections(
        html.encode(), file=Path("walmart-style-10k.html"), form_type="10-K"
    )

    assert result.status is ParseStatus.SUCCESS
    assert result.sections_found == 10
    assert result.diagnostics.ambiguous_item_classifications == []
    assert all(entry.anchor_id != "note-10" for entry in result.recognized_toc_entries)


def test_10k_normalizes_split_item_suffix_in_toc_row() -> None:
    html = (FIXTURES / "normal_10k.html").read_text(encoding="utf-8")
    html = html.replace(
        '<tr><td><a href="#properties">Item 2. Properties</a></td></tr>',
        """<tr><td>
          <a href="#cybersecurity">Item 1</a>
          <a href="#cybersecurity">C.</a>
          <a href="#cybersecurity">Cybersecurity</a>
        </td></tr>
        <tr><td><a href="#properties">Item 2. Properties</a></td></tr>""",
    ).replace(
        '<h2 id="properties">Item 2</h2>',
        '<h2 id="cybersecurity">Item 1C. Cybersecurity</h2><p>Cyber text.</p>\n'
        '<h2 id="properties">Item 2</h2>',
    )

    result = parse_filing_sections(
        html.encode(), file=Path("tesla-style-10k.html"), form_type="10-K"
    )

    assert result.status is ParseStatus.SUCCESS
    assert next(
        entry.anchor_id
        for entry in result.recognized_toc_entries
        if entry.section_id == "item1c_cybersecurity"
    ) == "cybersecurity"
    assert result.diagnostics.ambiguous_item_classifications == []


def test_10k_rejects_explanatory_toc_row_that_only_mentions_an_item() -> None:
    html = (FIXTURES / "normal_10k.html").read_text(encoding="utf-8")
    html = html.replace(
        "</table>",
        """<tr><td><a href="#part-three">
          (Except for information about executive officers in Item 1 above,
          Part III is incorporated by reference from the Proxy Statement.)
        </a></td></tr></table>""",
    ).replace(
        "</body>",
        '<h2 id="part-three">Part III</h2><p>Part III text.</p>\n</body>',
    )

    result = parse_filing_sections(
        html.encode(), file=Path("nike-style-10k.html"), form_type="10-K"
    )

    assert result.status is ParseStatus.SUCCESS
    assert result.diagnostics.ambiguous_item_classifications == []
    assert all(
        entry.anchor_id != "part-three" for entry in result.recognized_toc_entries
    )


def test_10k_prefers_exact_item_heading_over_earlier_combined_target() -> None:
    html = (FIXTURES / "normal_10k.html").read_text(encoding="utf-8")
    html = html.replace(
        '<tr><td><a href="#properties">Item 2. Properties</a></td></tr>',
        """<tr><td><a href="#combined-items">Item 1B. Unresolved Staff Comments</a></td></tr>
        <tr><td>
          <a href="#combined-items">Item 1</a><a href="#combined-items">C</a><a href="#combined-items">.</a>
          <a href="#cybersecurity">C</a><a href="#cybersecurity">ybersecurity.</a>
        </td></tr>
        <tr><td><a href="#properties">Item 2. Properties</a></td></tr>""",
    ).replace(
        '<h2 id="properties">Item 2</h2>',
        """<div id="combined-items">
          <h2>Item 1B. Unresolved Staff Comments</h2><p>None.</p>
          <h2 id="cybersecurity">Item 1C. Cybersecurity</h2><p>Cyber text.</p>
        </div>
        <h2 id="properties">Item 2</h2>""",
    )

    result = parse_filing_sections(
        html.encode(), file=Path("jpm-style-10k.html"), form_type="10-K"
    )

    assert result.status is ParseStatus.SUCCESS
    assert next(
        entry.anchor_id
        for entry in result.recognized_toc_entries
        if entry.section_id == "item1c_cybersecurity"
    ) == "cybersecurity"
    assert result.diagnostics.ambiguous_item_classifications == []


def test_10q_prefers_expected_destination_after_toc_prefix() -> None:
    html = (FIXTURES / "normal_10q.html").read_text(encoding="utf-8")
    html = html.replace(
        '<tr><td>Item 2. <a href="#mda">Management\'s Discussion and Analysis</a></td></tr>',
        """<tr><td>Item 2.
          <a href="#market-risk">Item 2.</a>
          <a href="#mda">Management's Discussion and Analysis</a>
          <a href="#mda">12</a>
        </td></tr>""",
    ).replace(
        '<h2 id="mda">Item 2. MD&amp;A</h2>',
        '<h2 id="mda">Table of Contents Item 2. Management\'s Discussion and Analysis</h2>',
    )

    result = parse_filing_sections(
        html.encode(), file=Path("nike-tesla-style-10q.html"), form_type="10-Q"
    )

    assert result.status is ParseStatus.SUCCESS
    mda = next(
        entry
        for entry in result.recognized_toc_entries
        if entry.section_id == "part1_item2_mda"
    )
    assert mda.anchor_id == "mda"
    assert result.diagnostics.ambiguous_item_classifications == []


def test_broken_split_link_is_nonfatal_when_sibling_resolves_section() -> None:
    html = (FIXTURES / "normal_10q.html").read_text(encoding="utf-8").replace(
        '<tr><td>Item 4. <a href="#controls">Controls and Procedures</a></td></tr>',
        """<tr><td>Item 4.
          <a href="#missing-item-fragment">Item 4.</a>
          <a href="#controls">Controls and Procedures</a>
        </td></tr>""",
    )

    result = parse_filing_sections(
        html.encode(), file=Path("broken-fragment-10q.html"), form_type="10-Q"
    )

    assert result.status is ParseStatus.SUCCESS
    assert "missing-item-fragment" in result.diagnostics.unresolved_anchor_ids
    assert "part1_item4_controls" not in result.diagnostics.unresolved_section_ids
    assert any(
        section.section_id == "part1_item4_controls" for section in result.sections
    )


def test_missing_toc_target_recovers_unique_exact_heading_anchor() -> None:
    html = (FIXTURES / "normal_10q.html").read_text(encoding="utf-8").replace(
        '<h2 id="risks">Item 1A. Risk Factors</h2>',
        '<a id="_opaque-risk-anchor"></a><p>Item 1A. Risk Factors</p>',
    )

    result = parse_filing_sections(
        html.encode(), file=Path("amc-style-10q.html"), form_type="10-Q"
    )

    assert result.status is ParseStatus.SUCCESS
    risk_factors = next(
        section
        for section in result.sections
        if section.section_id == "part2_item1a_risk_factors"
    )
    assert risk_factors.anchor_id == "_opaque-risk-anchor"
    assert result.diagnostics.unresolved_anchor_ids == []
    assert result.diagnostics.recovered_anchor_targets == [
        {
            "section_id": "part2_item1a_risk_factors",
            "missing_anchor_id": "risks",
            "recovered_anchor_id": "_opaque-risk-anchor",
            "method": "unique_exact_heading_adjacent_anchor",
        }
    ]


def test_missing_toc_target_does_not_guess_between_exact_headings() -> None:
    html = (FIXTURES / "normal_10q.html").read_text(encoding="utf-8").replace(
        '<h2 id="risks">Item 1A. Risk Factors</h2>',
        """<a id="_first-risk-anchor"></a><p>Item 1A. Risk Factors</p>
        <p>First risk discussion.</p>
        <a id="_second-risk-anchor"></a><p>Item 1A. Risk Factors</p>""",
    )

    result = parse_filing_sections(
        html.encode(), file=Path("ambiguous-heading-recovery-10q.html"), form_type="10-Q"
    )

    assert result.status is ParseStatus.PARTIAL
    assert result.diagnostics.recovered_anchor_targets == []
    assert "risks" in result.diagnostics.unresolved_anchor_ids
    assert "part2_item1a_risk_factors" in result.diagnostics.unresolved_section_ids


def test_10q_still_fails_when_destinations_have_equal_weak_evidence() -> None:
    html = (FIXTURES / "normal_10q.html").read_text(encoding="utf-8")
    html = html.replace(
        '<tr><td>Item 2. <a href="#mda">Management\'s Discussion and Analysis</a></td></tr>',
        """<tr><td>Item 2.
          <a href="#mda-a">Management's Discussion</a>
          <a href="#mda-b">Results of Operations</a>
        </td></tr>""",
    ).replace(
        '<h2 id="mda">Item 2. MD&amp;A</h2>',
        """<h2 id="mda-a">Operating overview</h2><p>First candidate.</p>
        <h2 id="mda-b">Quarterly highlights</h2><p>Second candidate.</p>""",
    )

    result = parse_filing_sections(
        html.encode(), file=Path("genuinely-ambiguous-10q.html"), form_type="10-Q"
    )

    assert result.status is ParseStatus.FAILURE
    assert result.failure_reason is FailureReason.AMBIGUOUS_ITEM_LINKS


def itemless_10k_html(*, business_caption: str = "Business") -> bytes:
    rows = [
        ("1", business_caption, "business", "1"),
        ("1A.", "Risk Factors", "risks", "2"),
        ("2", "Properties", "properties", "3"),
        ("3", "Legal Proceedings", "legal", "4"),
        ("5", "Market for Registrant's Common Equity and Related Stockholder Matters", "market", "5"),
        ("7", "Management's Discussion and Analysis of Financial Condition and Results of Operations", "mda", "6"),
        ("7A.", "Quantitative and Qualitative Disclosures About Market Risk", "market-risk", "7"),
        ("8", "Financial Statements and Supplementary Data", "financials", "8"),
    ]
    toc_rows = "".join(
        f"<tr><td><a href='#{anchor}'>{item}</a></td><td></td>"
        f"<td><a href='#{anchor}'>{caption}</a></td>"
        f"<td><a href='#{anchor}'>{page}</a></td></tr>"
        for item, caption, anchor, page in rows
    )
    targets = "".join(
        f"<h2 id='{anchor}'>Item {item.rstrip('.')} {caption}</h2><p>{caption} text.</p>"
        for item, caption, anchor, _ in rows
    )
    return (
        "<html><body><table>"
        + toc_rows
        + "<tr><td>General</td><td></td><td>General information</td><td>1</td></tr>"
        + "</table>"
        + targets
        + "</body></html>"
    ).encode()


def test_10k_uses_strict_itemless_toc_fallback() -> None:
    result = parse_filing_sections(
        itemless_10k_html(), file=Path("jnj-style-10k.html"), form_type="10-K"
    )

    assert result.status is ParseStatus.SUCCESS
    assert result.sections_found == 8
    assert result.diagnostics.itemless_toc_fallback_used is True
    assert {entry.item for entry in result.recognized_toc_entries} == {
        "1",
        "1A",
        "2",
        "3",
        "5",
        "7",
        "7A",
        "8",
    }


def test_itemless_toc_fallback_rejects_canonical_caption_mismatch() -> None:
    result = parse_filing_sections(
        itemless_10k_html(business_caption="Revenue by segment"),
        file=Path("numeric-table.html"),
        form_type="10-K",
    )

    assert result.status is ParseStatus.FAILURE
    assert result.failure_reason is FailureReason.NO_RECOGNIZED_ITEM_LINKS
    assert result.diagnostics.itemless_toc_fallback_used is False


@pytest.mark.parametrize("form_type", ["8-K", "20-F", ""])
def test_unknown_form_type_fails_clearly(form_type: str) -> None:
    result = parse_filing_sections(
        b"<html></html>", file=Path("filing.html"), form_type=form_type
    )
    assert result.failure_reason is FailureReason.UNKNOWN_FORM_TYPE
