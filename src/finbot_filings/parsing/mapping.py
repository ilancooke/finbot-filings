"""Optional canonical and semantic annotations for document-native sections."""

from __future__ import annotations

import re

from finbot_filings.parsing.definitions import FormDefinition, SectionDefinition

FINANCIAL_STATEMENT_PATTERN = re.compile(
    r"\bfinancial statements?(?: and supplementary data)?\b", re.IGNORECASE
)
RISK_FACTOR_PATTERN = re.compile(r"\brisk factors?\b", re.IGNORECASE)
MDA_PATTERN = re.compile(
    r"\bmanagement(?:'s|’s)? discussion and analysis\b|"
    r"\bresults of operations\b|\bliquidity and capital resources\b",
    re.IGNORECASE,
)
LEGAL_PATTERN = re.compile(r"\blegal proceedings?\b|\bcontingencies\b", re.IGNORECASE)
CYBERSECURITY_PATTERN = re.compile(r"\bcybersecurity\b", re.IGNORECASE)
MARKET_RISK_PATTERN = re.compile(r"\bmarket risk\b", re.IGNORECASE)
REGISTRANT_PATTERN = re.compile(
    r"\b(?:of|for)\s+(.+?)(?:\s+\d+)?$", re.IGNORECASE
)


def source_section_id(part: str | None, item: str | None, title: str = "") -> str:
    if item:
        normalized_item = re.sub(r"[^a-z0-9]+", "", item.lower())
        return f"{part}_item{normalized_item}" if part else f"item{normalized_item}"
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:60]
    return f"section_{slug or 'untitled'}"


def source_title_from_context(context: str, item: str) -> str:
    prefix = re.compile(
        rf"^\s*(?:part\s+(?:i|ii|iii|iv)\b\s*[.\-:–—]?\s*)?"
        rf"item\s+{re.escape(item[:-1] if item[-1:].isalpha() else item)}"
        rf"\s*{re.escape(item[-1]) if item[-1:].isalpha() else ''}"
        r"\b\s*[.\-:–—]?\s*",
        re.IGNORECASE,
    )
    title = prefix.sub("", context, count=1)
    title = re.sub(r"\s+\d+(?:\s*[-–—,]\s*\d+)*\s*$", "", title).strip()
    return title or f"Item {item}"


def canonical_definition(
    form: FormDefinition,
    *,
    part: str | None,
    item: str,
) -> SectionDefinition | None:
    return form.by_part_and_item.get((part, item))


def semantic_categories(
    *,
    title: str,
    canonical: SectionDefinition | None,
) -> tuple[str, ...]:
    categories: list[str] = []
    canonical_id = canonical.section_id if canonical else ""
    if "financial_statements" in canonical_id or FINANCIAL_STATEMENT_PATTERN.search(title):
        categories.append("financial_statements")
    if "risk_factors" in canonical_id or RISK_FACTOR_PATTERN.search(title):
        categories.append("risk_factors")
    if (
        canonical_id.endswith("_mda")
        or canonical_id == "item7_mda"
        or MDA_PATTERN.search(title)
    ):
        categories.append("management_discussion_and_analysis")
    if "legal_proceedings" in canonical_id or LEGAL_PATTERN.search(title):
        categories.append("legal_proceedings")
    if "cybersecurity" in canonical_id or CYBERSECURITY_PATTERN.search(title):
        categories.append("cybersecurity")
    if "market_risk" in canonical_id or MARKET_RISK_PATTERN.search(title):
        categories.append("market_risk")
    return tuple(categories)


def registrant_from_title(title: str) -> tuple[str | None, str | None]:
    if FINANCIAL_STATEMENT_PATTERN.search(title) is None:
        return None, None
    match = REGISTRANT_PATTERN.search(title)
    if match is None:
        return None, None
    name = match.group(1).strip(" .")
    return (name, "section_caption") if name else (None, None)
