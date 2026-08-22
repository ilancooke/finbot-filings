"""Canonical top-level SEC section definitions and confidence policies."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SectionDefinition:
    section_id: str
    part: str | None
    item: str
    title: str


@dataclass(frozen=True, slots=True)
class FormDefinition:
    form_type: str
    sections: tuple[SectionDefinition, ...]
    required_section_ids: frozenset[str]
    minimum_resolved_sections: int

    @property
    def by_part_and_item(self) -> dict[tuple[str | None, str], SectionDefinition]:
        return {(section.part, section.item): section for section in self.sections}


TEN_Q_SECTIONS = (
    SectionDefinition(
        "part1_item1_financial_statements",
        "part1",
        "1",
        "Financial Statements",
    ),
    SectionDefinition(
        "part1_item2_mda",
        "part1",
        "2",
        "Management's Discussion and Analysis of Financial Condition and Results of Operations",
    ),
    SectionDefinition(
        "part1_item3_market_risk",
        "part1",
        "3",
        "Quantitative and Qualitative Disclosures About Market Risk",
    ),
    SectionDefinition(
        "part1_item4_controls",
        "part1",
        "4",
        "Controls and Procedures",
    ),
    SectionDefinition(
        "part2_item1_legal_proceedings",
        "part2",
        "1",
        "Legal Proceedings",
    ),
    SectionDefinition(
        "part2_item1a_risk_factors",
        "part2",
        "1A",
        "Risk Factors",
    ),
    SectionDefinition(
        "part2_item2_unregistered_sales",
        "part2",
        "2",
        "Unregistered Sales of Equity Securities and Use of Proceeds",
    ),
    SectionDefinition(
        "part2_item3_defaults",
        "part2",
        "3",
        "Defaults Upon Senior Securities",
    ),
    SectionDefinition(
        "part2_item4_mine_safety",
        "part2",
        "4",
        "Mine Safety Disclosures",
    ),
    SectionDefinition(
        "part2_item5_other_information",
        "part2",
        "5",
        "Other Information",
    ),
    SectionDefinition(
        "part2_item6_exhibits",
        "part2",
        "6",
        "Exhibits",
    ),
)

TEN_K_SECTIONS = (
    SectionDefinition("item1_business", None, "1", "Business"),
    SectionDefinition("item1a_risk_factors", None, "1A", "Risk Factors"),
    SectionDefinition(
        "item1b_unresolved_staff_comments", None, "1B", "Unresolved Staff Comments"
    ),
    SectionDefinition("item1c_cybersecurity", None, "1C", "Cybersecurity"),
    SectionDefinition("item2_properties", None, "2", "Properties"),
    SectionDefinition("item3_legal_proceedings", None, "3", "Legal Proceedings"),
    SectionDefinition("item4_mine_safety", None, "4", "Mine Safety Disclosures"),
    SectionDefinition(
        "item5_market_for_equity",
        None,
        "5",
        "Market for Registrant's Common Equity and Related Stockholder Matters",
    ),
    SectionDefinition("item6_reserved", None, "6", "Reserved"),
    SectionDefinition(
        "item7_mda",
        None,
        "7",
        "Management's Discussion and Analysis of Financial Condition and Results of Operations",
    ),
    SectionDefinition(
        "item7a_market_risk",
        None,
        "7A",
        "Quantitative and Qualitative Disclosures About Market Risk",
    ),
    SectionDefinition(
        "item8_financial_statements",
        None,
        "8",
        "Financial Statements and Supplementary Data",
    ),
    SectionDefinition(
        "item9_accounting_changes",
        None,
        "9",
        "Changes in and Disagreements With Accountants",
    ),
    SectionDefinition("item9a_controls", None, "9A", "Controls and Procedures"),
    SectionDefinition("item9b_other_information", None, "9B", "Other Information"),
    SectionDefinition(
        "item9c_foreign_jurisdictions",
        None,
        "9C",
        "Disclosure Regarding Foreign Jurisdictions That Prevent Inspections",
    ),
    SectionDefinition(
        "item10_directors", None, "10", "Directors, Executive Officers and Governance"
    ),
    SectionDefinition("item11_executive_compensation", None, "11", "Executive Compensation"),
    SectionDefinition(
        "item12_security_ownership", None, "12", "Security Ownership of Certain Beneficial Owners"
    ),
    SectionDefinition(
        "item13_related_transactions",
        None,
        "13",
        "Certain Relationships and Related Transactions",
    ),
    SectionDefinition(
        "item14_accountant_fees", None, "14", "Principal Accountant Fees and Services"
    ),
    SectionDefinition(
        "item15_exhibits_schedules", None, "15", "Exhibits and Financial Statement Schedules"
    ),
    SectionDefinition("item16_summary", None, "16", "Form 10-K Summary"),
)

FORM_DEFINITIONS = {
    "10-Q": FormDefinition(
        form_type="10-Q",
        sections=TEN_Q_SECTIONS,
        required_section_ids=frozenset(
            {
                "part1_item1_financial_statements",
                "part1_item2_mda",
                "part1_item4_controls",
                "part2_item6_exhibits",
            }
        ),
        minimum_resolved_sections=6,
    ),
    "10-K": FormDefinition(
        form_type="10-K",
        sections=TEN_K_SECTIONS,
        required_section_ids=frozenset(
            {
                "item1_business",
                "item1a_risk_factors",
                "item7_mda",
                "item8_financial_statements",
            }
        ),
        minimum_resolved_sections=8,
    ),
}

