"""Deterministic extraction using only SEC Table of Contents internal anchors."""

from __future__ import annotations

import html
import re
import warnings as python_warnings
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from bs4 import BeautifulSoup, NavigableString, Tag, XMLParsedAsHTMLWarning

from finbot_filings.parsing.definitions import (
    FORM_DEFINITIONS,
    FormDefinition,
    SectionDefinition,
)
from finbot_filings.parsing.models import (
    ExtractedSection,
    FailureReason,
    FilingParseResult,
    ParseDiagnostics,
    ParseStatus,
    RecognizedTocEntry,
)
from finbot_filings.parsing.mapping import (
    registrant_from_title,
    semantic_categories,
    source_section_id,
    source_title_from_context,
)

ITEM_ROW_PATTERN = re.compile(
    r"^\s*(?:part\s+(?:i|ii|iii|iv)\b\s*[.\-:–—]?\s*)?"
    r"item\s+(\d{1,2}[a-z]?)\b\s*[.\-:–—]?",
    re.IGNORECASE,
)
SPLIT_ITEM_SUFFIX_PATTERN = re.compile(
    r"(\bitem\s+\d{1,2})\s+([abc])(?=\s*[.\-:–—])",
    re.IGNORECASE,
)
PART_PATTERN = re.compile(r"\bpart\s+(ii|i)\b", re.IGNORECASE)
PART_ROW_PATTERN = re.compile(r"^\s*part\s+(ii|i)\b", re.IGNORECASE)
TOC_PATTERN = re.compile(r"\btable\s+of\s+contents\b", re.IGNORECASE)
LEADING_TOC_PATTERN = re.compile(
    r"^(?:\s*table\s+of\s+contents\b[\s:|\-–—]*)+", re.IGNORECASE
)
WHITESPACE_PATTERN = re.compile(r"\s+")
NUMERIC_PAGE_LINK_PATTERN = re.compile(r"^\d+(?:\s*[-–—,]\s*\d+)*$")
ITEMLESS_ITEM_PATTERN = re.compile(r"^(\d{1,2}[a-c]?)\.?$", re.IGNORECASE)
CAPTION_WORD_PATTERN = re.compile(r"[a-z0-9]+")
CAPTION_STOP_WORDS = frozenset(
    {"and", "of", "the", "for", "in", "to", "with", "certain"}
)
HIDDEN_STYLE_PATTERN = re.compile(
    r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", re.IGNORECASE
)
IGNORED_TEXT_ANCESTORS = frozenset(
    {"head", "script", "style", "noscript", "ix:header", "ix:hidden"}
)


def _normalize_text(value: str) -> str:
    return WHITESPACE_PATTERN.sub(" ", html.unescape(value).replace("\xa0", " ")).strip()


def _link_context(link: Tag) -> str:
    row = link.find_parent("tr")
    if row is not None:
        return _normalize_text(row.get_text(" ", strip=True))
    block = link.find_parent(["p", "li", "div"])
    if block is not None:
        text = _normalize_text(block.get_text(" ", strip=True))
        if len(text) <= 1_000:
            return text
    return _normalize_text(link.get_text(" ", strip=True))


def _item_row_match(value: str) -> re.Match[str] | None:
    normalized = SPLIT_ITEM_SUFFIX_PATTERN.sub(r"\1\2", value)
    return ITEM_ROW_PATTERN.search(normalized)


def _part_from_text(value: str) -> str | None:
    match = PART_PATTERN.search(value)
    if match is None:
        return None
    return "part2" if match.group(1).upper() == "II" else "part1"


def _part_marker_from_row(value: str) -> str | None:
    match = PART_ROW_PATTERN.search(value)
    if match is None:
        return None
    return "part2" if match.group(1).upper() == "II" else "part1"


def _toc_parts_by_row(toc_links: list[Tag]) -> dict[int, str]:
    """Map every row in selected TOC tables to its preceding native Part marker."""
    parts_by_row: dict[int, str] = {}
    tables: dict[int, Tag] = {}
    for link in toc_links:
        table = link.find_parent("table")
        if table is not None:
            tables[id(table)] = table

    for table in tables.values():
        current_part: str | None = None
        for row in table.find_all("tr"):
            marker = _part_marker_from_row(_normalize_text(row.get_text(" ", strip=True)))
            if marker is not None:
                current_part = marker
            if current_part is not None:
                parts_by_row[id(row)] = current_part
    return parts_by_row


def _target_heading_text(target: Tag) -> str:
    chunks: list[str] = []
    for element in target.next_elements:
        if isinstance(element, NavigableString) and not _is_hidden_text(element):
            text = _normalize_text(str(element))
            if text:
                chunks.append(text)
        if len(" ".join(chunks)) >= 500:
            break
    return LEADING_TOC_PATTERN.sub("", " ".join(chunks), count=1).strip()


def _target_item_evidence(
    target: Tag, expected_item: str, expected_title: str
) -> int:
    """Score a destination that starts with the expected filing heading.

    Inline XBRL filings often repeat ``Table of Contents`` immediately before a
    section heading. That presentational prefix is ignored, but the Item heading
    must still occur at the start of the destination text. A matching caption is
    stronger evidence than the Item number alone.
    """
    target_text = _target_heading_text(target)
    match = _item_row_match(target_text)
    if match is None or match.group(1).upper() != expected_item:
        return 0

    target_words = " ".join(CAPTION_WORD_PATTERN.findall(target_text.lower()))
    title_words = " ".join(CAPTION_WORD_PATTERN.findall(expected_title.lower()))
    return 2 if title_words and title_words in target_words else 1


def _link_evidence_priority(
    link: Tag, target: Tag, expected_item: str, expected_title: str
) -> tuple[int, int]:
    """Rank destination evidence first and exact link text second.

    Page-number links remain usable when they are the only links available for a
    TOC row, but they do not create ambiguity when a nonnumeric link resolves the
    same canonical section to a different target. When semantic links disagree,
    an anchor whose destination starts with the expected Item heading wins. If
    destinations have equal heading evidence, a direct Item or exact-caption
    link wins over a Part label, generic semantic text, or page number.
    """
    target_evidence = _target_item_evidence(target, expected_item, expected_title)
    link_text = _normalize_text(link.get_text(" ", strip=True))
    if NUMERIC_PAGE_LINK_PATTERN.fullmatch(link_text):
        link_evidence = 0
    else:
        item_match = _item_row_match(link_text)
        direct_item = (
            item_match is not None
            and item_match.group(1).upper() == expected_item
        )
        link_key = _heading_key(link_text)
        title_key = _heading_key(expected_title)
        exact_caption = bool(title_key) and link_key == title_key
        link_evidence = 2 if direct_item or exact_caption else 1
    return target_evidence, link_evidence


def _heading_key(value: str) -> str:
    return " ".join(CAPTION_WORD_PATTERN.findall(value.lower()))


def _recover_missing_toc_target(
    soup: BeautifulSoup,
    *,
    link_position: int,
    expected_item: str,
    expected_title: str,
    dom_positions: dict[int, int],
) -> tuple[str, Tag] | None:
    """Recover one broken TOC target from a unique exact adjacent heading anchor.

    This is deliberately not general heading-based section discovery. It runs
    only after a recognized TOC link is missing and requires an exact Item plus
    caption heading outside a table. The heading must either carry another ID or
    be immediately preceded by an ID-bearing anchor.
    """
    expected_key = _heading_key(f"Item {expected_item} {expected_title}")
    candidates: list[tuple[str, Tag]] = []
    for heading in soup.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6"]):
        if heading.find_parent("table") is not None:
            continue
        visible_text = " ".join(
            _normalize_text(str(node))
            for node in heading.descendants
            if isinstance(node, NavigableString) and not _is_hidden_text(node)
        )
        if _heading_key(visible_text) != expected_key:
            continue

        target = heading if heading.get("id") else heading.find_previous_sibling()
        if not isinstance(target, Tag) or not target.get("id"):
            continue
        if target is not heading and (
            target.name != "a" or _normalize_text(target.get_text(" ", strip=True))
        ):
            continue
        target_position = dom_positions.get(id(target), -1)
        if target_position <= link_position:
            continue
        candidates.append((str(target.get("id")), target))

    unique_candidates = {
        (anchor_id, dom_positions[id(target)]): (anchor_id, target)
        for anchor_id, target in candidates
    }
    return next(iter(unique_candidates.values())) if len(unique_candidates) == 1 else None


def _discover_toc_links(
    soup: BeautifulSoup,
    internal_links: list[Tag],
    dom_positions: dict[int, int],
) -> list[Tag]:
    """Return links from the strongest deterministic TOC region.

    SEC filing bodies commonly contain linked Item cross-references. Those links
    are not section-discovery evidence. Prefer table regions containing a sequence
    of distinct Item rows and forward section targets. If no such table exists,
    use the bounded region after an explicit Table of Contents label.
    """
    targets_by_id = {
        str(tag.get("id")): tag
        for tag in soup.find_all(True)
        if tag.get("id") is not None
    }
    toc_text = soup.find(string=TOC_PATTERN)
    if toc_text is not None and isinstance(toc_text.parent, Tag):
        marker_position = dom_positions[id(toc_text.parent)]
        for table in toc_text.parent.find_all_next("table", limit=3):
            table_links = [
                link
                for link in internal_links
                if link.find_parent("table") is table
            ]
            forward_links = []
            for link in table_links:
                anchor_id = unquote(str(link.get("href", ""))[1:]).strip()
                target = targets_by_id.get(anchor_id)
                if (
                    target is not None
                    and dom_positions[id(target)] > dom_positions[id(link)]
                ):
                    forward_links.append(link)
            if len(forward_links) >= 4:
                if not any(_item_row_match(_link_context(link)) for link in table_links):
                    return table_links
                # This is an Item-based outline. Let the normal TOC scoring
                # select its complete table instead of mistaking a following
                # financial-statement sub-index for the filing outline.
                break
        links_after_marker = [
            link for link in internal_links if dom_positions[id(link)] > marker_position
        ]
        forward_item_targets = []
        for link in links_after_marker:
            if _item_row_match(_link_context(link)) is None:
                continue
            anchor_id = unquote(str(link.get("href", ""))[1:]).strip()
            target = targets_by_id.get(anchor_id)
            if target is not None and dom_positions[id(target)] > dom_positions[id(link)]:
                forward_item_targets.append(dom_positions[id(target)])
        if forward_item_targets:
            first_section_target = min(forward_item_targets)
            return [
                link
                for link in links_after_marker
                if dom_positions[id(link)] < first_section_target
            ]

    table_groups: dict[int, tuple[Tag, list[Tag]]] = {}
    for link in internal_links:
        table = link.find_parent("table")
        if table is None:
            continue
        key = id(table)
        if key not in table_groups:
            table_groups[key] = (table, [])
        table_groups[key][1].append(link)

    candidates: list[tuple[tuple[int, int, int, int, int], int, list[Tag]]] = []
    for table_key, (_, links) in table_groups.items():
        item_links = [link for link in links if _item_row_match(_link_context(link))]
        distinct_items = {
            match.group(1).upper()
            for link in item_links
            if (match := _item_row_match(_link_context(link))) is not None
        }
        if len(distinct_items) < 2:
            continue
        forward_targets = []
        for link in item_links:
            anchor_id = unquote(str(link.get("href", ""))[1:]).strip()
            target = targets_by_id.get(anchor_id)
            if target is not None and dom_positions[id(target)] > dom_positions[id(link)]:
                forward_targets.append(dom_positions[id(target)])
        first_source = min(dom_positions[id(link)] for link in links)
        score = (
            int(bool(forward_targets)),
            len(distinct_items),
            len(forward_targets),
            len(item_links),
            -first_source,
        )
        candidates.append((score, table_key, forward_targets))

    if candidates:
        _, best_table_key, best_forward_targets = max(candidates, key=lambda value: value[0])
        selected_table_keys = {best_table_key}
        if best_forward_targets:
            first_section_target = min(best_forward_targets)
            selected_table_keys = {
                table_key
                for _, table_key, forward_targets in candidates
                if forward_targets
                and min(
                    dom_positions[id(link)]
                    for link in table_groups[table_key][1]
                )
                < first_section_target
            }
        return [
            link
            for link in internal_links
            if (table := link.find_parent("table")) is not None
            and id(table) in selected_table_keys
        ]

    return []


def _inline_text(tag: Tag) -> str:
    return _normalize_text("".join(str(value) for value in tag.strings))


def _itemless_row_title(row: Tag) -> str:
    cells = row.find_all(["td", "th"], recursive=False)
    return _normalize_text(" ".join(_inline_text(cell) for cell in cells[1:-1]))


def _generic_source_title(context: str) -> str:
    title = re.sub(
        r"\s+\d+(?:\s*[-–—,]\s*\d+)*\s*$", "", context
    ).strip()
    return title or "Untitled Section"


def _caption_matches_definition(caption: str, definition: SectionDefinition) -> bool:
    caption_words = set(CAPTION_WORD_PATTERN.findall(caption.lower())) - CAPTION_STOP_WORDS
    definition_words = (
        set(CAPTION_WORD_PATTERN.findall(definition.title.lower())) - CAPTION_STOP_WORDS
    )
    if not caption_words or not definition_words:
        return False
    return len(caption_words & definition_words) / len(definition_words) >= 0.75


def _discover_itemless_10k_toc_links(
    soup: BeautifulSoup,
    internal_links: list[Tag],
    dom_positions: dict[int, int],
    form: FormDefinition,
) -> tuple[list[Tag], dict[int, SectionDefinition]]:
    """Find a strongly validated 10-K TOC whose rows omit the word Item."""
    if form.form_type != "10-K":
        return [], {}

    definitions = {definition.item: definition for definition in form.sections}
    targets_by_id = {
        str(tag.get("id")): tag
        for tag in soup.find_all(True)
        if tag.get("id") is not None
    }
    rows_by_table: dict[int, dict[int, SectionDefinition]] = {}
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"], recursive=False)
        if len(cells) < 3:
            continue
        item_match = ITEMLESS_ITEM_PATTERN.fullmatch(_inline_text(cells[0]))
        if item_match is None:
            continue
        item = item_match.group(1).upper()
        definition = definitions.get(item)
        if definition is None:
            continue
        page_text = _inline_text(cells[-1])
        if NUMERIC_PAGE_LINK_PATTERN.fullmatch(page_text) is None:
            continue
        caption = _normalize_text(" ".join(_inline_text(cell) for cell in cells[1:-1]))
        if not _caption_matches_definition(caption, definition):
            continue

        has_exact_forward_target = False
        for link in row.select('a[href^="#"]'):
            anchor_id = unquote(str(link.get("href", ""))[1:]).strip()
            target = targets_by_id.get(anchor_id)
            if (
                target is not None
                and dom_positions[id(target)] > dom_positions[id(link)]
                and _target_item_evidence(target, item, "")
            ):
                has_exact_forward_target = True
                break
        if not has_exact_forward_target:
            continue
        table = row.find_parent("table")
        if table is None:
            continue
        rows_by_table.setdefault(id(table), {})[id(row)] = definition

    qualified = []
    for table_key, row_definitions in rows_by_table.items():
        section_ids = {
            definition.section_id for definition in row_definitions.values()
        }
        if (
            len(section_ids) >= form.minimum_resolved_sections
            and form.required_section_ids.issubset(section_ids)
        ):
            qualified.append((len(section_ids), table_key, row_definitions))
    if not qualified:
        return [], {}

    _, _, selected_rows = max(qualified, key=lambda value: value[0])
    selected_links = [
        link
        for link in internal_links
        if (row := link.find_parent("tr")) is not None and id(row) in selected_rows
    ]
    return selected_links, selected_rows


def _caption_definition(
    context: str,
    item: str,
    form: FormDefinition,
) -> SectionDefinition | None:
    """Return a unique 10-Q definition whose canonical caption appears in the TOC row."""
    normalized_context = re.sub(r"[^a-z0-9]+", " ", context.lower()).strip()
    matches = []
    for definition in form.sections:
        if definition.item != item:
            continue
        normalized_title = re.sub(
            r"[^a-z0-9]+", " ", definition.title.lower()
        ).strip()
        if normalized_title and normalized_title in normalized_context:
            matches.append(definition)
    return matches[0] if len(matches) == 1 else None


def _is_hidden_text(node: NavigableString) -> bool:
    for parent in node.parents:
        if not isinstance(parent, Tag):
            continue
        name = (parent.name or "").lower()
        if name in IGNORED_TEXT_ANCESTORS:
            return True
        if parent.has_attr("hidden") or str(parent.get("aria-hidden", "")).lower() == "true":
            return True
        if HIDDEN_STYLE_PATTERN.search(str(parent.get("style", ""))):
            return True
    return False


def _extract_text_between(start: Tag, end: Tag | None) -> str:
    chunks: list[str] = []
    for element in start.next_elements:
        if end is not None and element is end:
            break
        if isinstance(element, NavigableString) and not _is_hidden_text(element):
            text = _normalize_text(str(element))
            if text:
                chunks.append(text)
    return _normalize_text(" ".join(chunks))


def _failure(
    file: Path,
    form_type: str,
    reason: FailureReason,
    details: str,
    diagnostics: ParseDiagnostics,
    entries: list[RecognizedTocEntry] | None = None,
    warnings: list[str] | None = None,
) -> FilingParseResult:
    return FilingParseResult(
        file=file,
        form_type=form_type,
        status=ParseStatus.FAILURE,
        recognized_toc_entries=entries or [],
        diagnostics=diagnostics,
        warnings=warnings or [],
        failure_reason=reason,
        details=details,
    )


def _classify_toc_entries(
    soup: BeautifulSoup,
    form: FormDefinition,
    diagnostics: ParseDiagnostics,
) -> tuple[list[RecognizedTocEntry], dict[str, Tag], list[str]]:
    all_tags = list(soup.find_all(True))
    dom_positions = {id(tag): index for index, tag in enumerate(all_tags)}
    definitions = form.by_part_and_item
    entries_by_section: dict[str, RecognizedTocEntry] = {}
    priorities_by_section: dict[str, tuple[int, int]] = {}
    link_texts_by_section: dict[str, str] = {}
    targets_by_section: dict[str, Tag] = {}
    warnings: list[str] = []
    current_part: str | None = None
    source_id_counts: dict[str, int] = {}
    source_ids_by_row: dict[int, str] = {}
    unresolved_native_sections: dict[str, str] = {}

    internal_links = [
        link
        for link in soup.find_all("a", href=True)
        if str(link.get("href", "")).strip().startswith("#")
    ]
    diagnostics.internal_links_inspected = len(internal_links)
    toc_links = _discover_toc_links(soup, internal_links, dom_positions)
    itemless_definitions: dict[int, SectionDefinition] = {}
    if not toc_links:
        toc_links, itemless_definitions = _discover_itemless_10k_toc_links(
            soup, internal_links, dom_positions, form
        )
        diagnostics.itemless_toc_fallback_used = bool(toc_links)
    parts_by_row = _toc_parts_by_row(toc_links)
    native_topic_outline = bool(toc_links) and not itemless_definitions and not any(
        _item_row_match(_link_context(link)) for link in toc_links
    )
    diagnostics.toc_like_region_found = bool(toc_links)
    detected_outline_entries: dict[str, str] = {}
    candidate_failure_reasons: dict[str, set[str]] = {}
    ambiguous_native_ids: set[str] = set()
    ambiguities_by_native_id: dict[str, list[str]] = {}

    for toc_order, link in enumerate(toc_links):
        href = str(link.get("href", "")).strip()
        context = _link_context(link)
        row = link.find_parent("tr")
        canonical = itemless_definitions.get(id(row)) if row is not None else None
        if canonical is not None:
            item = canonical.item
            part = canonical.part
            source_title = _itemless_row_title(row)
        else:
            explicit_part = _part_from_text(context)
            if explicit_part is not None and _item_row_match(context) is None:
                current_part = explicit_part
                continue

            item_match = _item_row_match(context)
            if item_match is None:
                if explicit_part is not None:
                    current_part = explicit_part
                    continue
                if not native_topic_outline:
                    continue
                item = None
                part = current_part if form.form_type == "10-Q" else None
                canonical = None
                link_text = _normalize_text(link.get_text(" ", strip=True))
                source_title = _generic_source_title(
                    context
                    if NUMERIC_PAGE_LINK_PATTERN.fullmatch(link_text)
                    else link_text
                )
                if TOC_PATTERN.fullmatch(source_title):
                    continue
            else:
                item = item_match.group(1).upper()
                structural_part = parts_by_row.get(id(row)) if row is not None else None
                part = (
                    explicit_part or structural_part or current_part
                    if form.form_type == "10-Q"
                    else None
                )
                if structural_part is not None:
                    current_part = structural_part
                caption_definition = (
                    _caption_definition(context, item, form)
                    if (
                        form.form_type == "10-Q"
                        and explicit_part is None
                        and structural_part is None
                    )
                    else None
                )
                canonical = caption_definition or definitions.get((part, item))
                if caption_definition is not None:
                    part = caption_definition.part
                    current_part = part
                    assignment = f"{caption_definition.section_id}: {context[:300]}"
                    if assignment not in diagnostics.caption_based_part_assignments:
                        diagnostics.caption_based_part_assignments.append(assignment)
                if canonical is None and form.form_type == "10-Q":
                    if part is None:
                        diagnostics.ambiguous_item_classifications.append(context[:300])
                source_title = source_title_from_context(context, item)

        candidate_key = id(row) if row is not None and item is not None else id(link)
        base_native_id = source_section_id(part, item, source_title)
        row_key = candidate_key
        existing_for_row = source_ids_by_row.get(row_key)
        if existing_for_row is not None:
            native_id = existing_for_row
        elif item is not None and part is not None:
            native_id = base_native_id
            source_ids_by_row[row_key] = native_id
        else:
            count = source_id_counts.get(base_native_id, 0) + 1
            source_id_counts[base_native_id] = count
            native_id = base_native_id if count == 1 else f"{base_native_id}_{count}"
            source_ids_by_row[row_key] = native_id
        detected_outline_entries.setdefault(native_id, context[:300])
        canonical_id = canonical.section_id if canonical is not None else None
        section_id = canonical_id or native_id
        categories = semantic_categories(title=source_title, canonical=canonical)
        registrant_name, registrant_source = registrant_from_title(source_title)

        diagnostics.item_links_classified += 1
        anchor_id = unquote(href[1:]).strip()
        target = soup.find(id=anchor_id) if anchor_id else None
        if not isinstance(target, Tag):
            recovery = (
                _recover_missing_toc_target(
                    soup,
                    link_position=dom_positions.get(id(link), -1),
                    expected_item=item,
                    expected_title=source_title,
                    dom_positions=dom_positions,
                )
                if item is not None
                else None
            )
            if recovery is None:
                if anchor_id and anchor_id not in diagnostics.unresolved_anchor_ids:
                    diagnostics.unresolved_anchor_ids.append(anchor_id)
                unresolved_native_sections[native_id] = canonical_id or native_id
                candidate_failure_reasons.setdefault(native_id, set()).add(
                    "missing_anchor_target"
                )
                continue
            missing_anchor_id = anchor_id
            anchor_id, target = recovery
            href = f"#{anchor_id}"
            recovery_record = {
                "section_id": section_id,
                "missing_anchor_id": missing_anchor_id,
                "recovered_anchor_id": anchor_id,
                "method": "unique_exact_heading_adjacent_anchor",
            }
            if recovery_record not in diagnostics.recovered_anchor_targets:
                diagnostics.recovered_anchor_targets.append(recovery_record)

        link_position = dom_positions.get(id(link), -1)
        target_position = dom_positions.get(id(target), -1)
        if target_position <= link_position:
            if anchor_id not in diagnostics.non_forward_anchor_ids:
                diagnostics.non_forward_anchor_ids.append(anchor_id)
            candidate_failure_reasons.setdefault(native_id, set()).add(
                "non_forward_anchor"
            )
            continue
        diagnostics.valid_anchor_targets += 1
        entry = RecognizedTocEntry(
            section_id=section_id,
            part=part,
            item=item,
            title=source_title,
            anchor_id=anchor_id,
            href=href,
            toc_text=context,
            toc_order=toc_order,
            dom_order=target_position,
            source_section_id=native_id,
            source_title=source_title,
            canonical_section_id=canonical_id,
            canonical_mapping_method=(
                "deterministic_item_and_caption" if canonical_id else None
            ),
            semantic_categories=categories,
            registrant_name=registrant_name,
            registrant_identity_source=registrant_source,
        )
        priority = (
            _link_evidence_priority(link, target, item, source_title)
            if item is not None
            else (
                (0, 0)
                if NUMERIC_PAGE_LINK_PATTERN.fullmatch(
                    _normalize_text(link.get_text(" ", strip=True))
                )
                else (0, 1)
            )
        )
        link_text = _normalize_text(link.get_text(" ", strip=True))
        existing = entries_by_section.get(native_id)
        if existing is not None:
            existing_priority = priorities_by_section[native_id]
            if existing.anchor_id == anchor_id:
                if priority > existing_priority:
                    priorities_by_section[native_id] = priority
                    link_texts_by_section[native_id] = link_text
                continue
            if priority != existing_priority:
                chosen = entry if priority > existing_priority else existing
                rejected = existing if priority > existing_priority else entry
                chosen_priority = max(priority, existing_priority)
                rejected_priority = min(priority, existing_priority)
                chosen_link_text = (
                    link_text
                    if priority > existing_priority
                    else link_texts_by_section[native_id]
                )
                rejected_link_text = (
                    link_texts_by_section[native_id]
                    if priority > existing_priority
                    else link_text
                )
                if chosen_priority[0] == rejected_priority[0]:
                    record = {
                        "section_id": section_id,
                        "source_section_id": native_id,
                        "chosen_anchor_id": chosen.anchor_id,
                        "rejected_anchor_id": rejected.anchor_id,
                        "chosen_destination_evidence": chosen_priority[0],
                        "chosen_link_text_evidence": chosen_priority[1],
                        "rejected_destination_evidence": rejected_priority[0],
                        "rejected_link_text_evidence": rejected_priority[1],
                        "chosen_link_text": chosen_link_text,
                        "rejected_link_text": rejected_link_text,
                        "method": "direct_item_or_caption_link_tiebreak",
                    }
                    duplicate = any(
                        value["source_section_id"] == native_id
                        and value["chosen_anchor_id"] == chosen.anchor_id
                        and value["rejected_anchor_id"] == rejected.anchor_id
                        for value in diagnostics.disambiguated_item_links
                    )
                    if not duplicate:
                        diagnostics.disambiguated_item_links.append(record)
                if priority < existing_priority:
                    continue
                ambiguous_native_ids.discard(native_id)
                candidate_failure_reasons.get(native_id, set()).discard(
                    "ambiguous_anchor_targets"
                )
                entries_by_section[native_id] = entry
                priorities_by_section[native_id] = priority
                link_texts_by_section[native_id] = link_text
                targets_by_section[native_id] = target
                continue
            ambiguities_by_native_id.setdefault(native_id, []).append(
                f"{section_id}: {existing.anchor_id}, {anchor_id}"
            )
            ambiguous_native_ids.add(native_id)
            candidate_failure_reasons.setdefault(native_id, set()).add(
                "ambiguous_anchor_targets"
            )
            continue
        entries_by_section[native_id] = entry
        priorities_by_section[native_id] = priority
        link_texts_by_section[native_id] = link_text
        targets_by_section[native_id] = target

    extracted_native_ids = set(entries_by_section) - ambiguous_native_ids
    diagnostics.ambiguous_item_classifications.extend(
        message
        for native_id in ambiguous_native_ids
        for message in ambiguities_by_native_id.get(native_id, [])
    )
    diagnostics.native_outline_entries_detected = len(detected_outline_entries)
    diagnostics.native_outline_entries_extracted = len(extracted_native_ids)
    diagnostics.native_outline_entries_skipped.extend(
        {
            "toc_text": context,
            "reason": ",".join(
                sorted(candidate_failure_reasons.get(native_id, {"not_selected"}))
            ),
        }
        for native_id, context in detected_outline_entries.items()
        if native_id not in extracted_native_ids
    )
    diagnostics.unresolved_section_ids.extend(
        section_id
        for native_id, section_id in unresolved_native_sections.items()
        if native_id not in entries_by_section
    )
    entries = sorted(entries_by_section.values(), key=lambda entry: entry.dom_order)
    if diagnostics.unresolved_anchor_ids:
        warnings.append(
            f"{len(diagnostics.unresolved_anchor_ids)} recognized TOC anchor target(s) were missing"
        )
    if diagnostics.non_forward_anchor_ids:
        warnings.append(
            f"{len(diagnostics.non_forward_anchor_ids)} recognized link(s) pointed backward and were ignored"
        )
    return entries, targets_by_section, warnings


def parse_filing_sections(
    html_bytes: bytes,
    *,
    file: Path,
    form_type: str,
) -> FilingParseResult:
    """Parse top-level sections using resolved TOC anchors and physical DOM order.

    Document-native titles and boundaries are primary. Canonical SEC Item IDs and
    semantic categories are optional annotations and do not control extraction.
    Success requires a credible TOC with at least the form-specific minimum number
    of forward, resolvable section anchors. Missing targets produce partial output
    when no alternate link resolves that same section and enough other boundaries
    remain usable.
    """
    normalized_form = form_type.strip().upper()
    diagnostics = ParseDiagnostics()
    form = FORM_DEFINITIONS.get(normalized_form)
    if form is None:
        return _failure(
            file,
            normalized_form,
            FailureReason.UNKNOWN_FORM_TYPE,
            f"TOC parsing supports only 10-Q and 10-K, not {form_type!r}",
            diagnostics,
        )

    try:
        with python_warnings.catch_warnings():
            python_warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
            soup = BeautifulSoup(html_bytes, "lxml")
        diagnostics.toc_like_region_found = soup.find(string=TOC_PATTERN) is not None
        entries, targets, parse_warnings = _classify_toc_entries(
            soup, form, diagnostics
        )
        diagnostics.toc_like_region_found = (
            diagnostics.toc_like_region_found
            or diagnostics.item_links_classified >= 2
        )
    except Exception as exc:
        return _failure(
            file,
            normalized_form,
            FailureReason.PARSE_ERROR,
            f"HTML parsing failed: {exc}",
            diagnostics,
        )

    if diagnostics.internal_links_inspected == 0:
        return _failure(
            file,
            normalized_form,
            FailureReason.NO_TOC_FOUND,
            "The filing contains no internal HTML links.",
            diagnostics,
        )
    if diagnostics.item_links_classified == 0:
        reason = (
            FailureReason.AMBIGUOUS_PART_ASSIGNMENT
            if diagnostics.ambiguous_item_classifications
            else FailureReason.NO_RECOGNIZED_ITEM_LINKS
        )
        return _failure(
            file,
            normalized_form,
            reason,
            "No internal links could be classified as supported top-level SEC Items.",
            diagnostics,
        )
    if not entries and diagnostics.unresolved_anchor_ids:
        return _failure(
            file,
            normalized_form,
            FailureReason.ANCHOR_TARGET_MISSING,
            "Recognized Item links were found, but none resolved to forward DOM targets.",
            diagnostics,
            warnings=parse_warnings,
        )

    resolved_ids = {
        entry.canonical_section_id
        for entry in entries
        if entry.canonical_section_id is not None
    }
    diagnostics.unmapped_reference_sections = sorted(
        form.required_section_ids - resolved_ids
    )
    if diagnostics.ambiguous_item_classifications:
        conflicting = [
            value for value in diagnostics.ambiguous_item_classifications if ":" in value
        ]
        if conflicting:
            return _failure(
                file,
                normalized_form,
                FailureReason.AMBIGUOUS_ITEM_LINKS,
                "A canonical section resolved to multiple different anchor targets.",
                diagnostics,
                entries,
                parse_warnings,
            )
    if len(entries) < form.minimum_resolved_sections:
        reason = (
            FailureReason.AMBIGUOUS_PART_ASSIGNMENT
            if (
                normalized_form == "10-Q"
                and diagnostics.ambiguous_item_classifications
            )
            else FailureReason.INSUFFICIENT_SECTION_ANCHORS
        )
        return _failure(
            file,
            normalized_form,
            reason,
            f"Resolved {len(entries)} sections; at least "
            f"{form.minimum_resolved_sections} are required.",
            diagnostics,
            entries,
            parse_warnings,
        )

    sections: list[ExtractedSection] = []
    for index, entry in enumerate(entries):
        target = targets[entry.source_section_id]
        next_target = (
            targets[entries[index + 1].source_section_id]
            if index + 1 < len(entries)
            else None
        )
        sections.append(
            ExtractedSection(
                section_id=entry.section_id,
                part=entry.part,
                item=entry.item,
                title=entry.title,
                anchor_id=entry.anchor_id,
                physical_order=index + 1,
                text=_extract_text_between(target, next_target),
                source_section_id=entry.source_section_id,
                source_title=entry.source_title,
                canonical_section_id=entry.canonical_section_id,
                canonical_mapping_method=entry.canonical_mapping_method,
                semantic_categories=entry.semantic_categories,
                registrant_name=entry.registrant_name,
                registrant_identity_source=entry.registrant_identity_source,
            )
        )

    return FilingParseResult(
        file=file,
        form_type=normalized_form,
        status=(
            ParseStatus.PARTIAL
            if (
                diagnostics.unresolved_section_ids
                or diagnostics.native_outline_entries_skipped
            )
            else ParseStatus.SUCCESS
        ),
        sections=sections,
        recognized_toc_entries=entries,
        diagnostics=diagnostics,
        warnings=parse_warnings,
    )
