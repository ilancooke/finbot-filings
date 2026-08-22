"""Typed results for deterministic filing section extraction."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class ParseStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"


class MappingStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NONE = "none"


class FailureReason(StrEnum):
    UNKNOWN_FORM_TYPE = "unknown_form_type"
    NO_TOC_FOUND = "no_toc_found"
    NO_RECOGNIZED_ITEM_LINKS = "no_recognized_item_links"
    ANCHOR_TARGET_MISSING = "anchor_target_missing"
    AMBIGUOUS_PART_ASSIGNMENT = "ambiguous_part_assignment"
    AMBIGUOUS_ITEM_LINKS = "ambiguous_item_links"
    INSUFFICIENT_SECTION_ANCHORS = "insufficient_section_anchors"
    STORAGE_LAYOUT_MISMATCH = "storage_layout_mismatch"
    PARSE_ERROR = "parse_error"


@dataclass(frozen=True, slots=True)
class RecognizedTocEntry:
    section_id: str
    part: str | None
    item: str | None
    title: str
    anchor_id: str
    href: str
    toc_text: str
    toc_order: int
    dom_order: int
    source_section_id: str
    source_title: str
    canonical_section_id: str | None = None
    canonical_mapping_method: str | None = None
    semantic_categories: tuple[str, ...] = ()
    registrant_name: str | None = None
    registrant_identity_source: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractedSection:
    section_id: str
    part: str | None
    item: str | None
    title: str
    anchor_id: str
    physical_order: int
    text: str
    source_section_id: str
    source_title: str
    canonical_section_id: str | None = None
    canonical_mapping_method: str | None = None
    semantic_categories: tuple[str, ...] = ()
    registrant_name: str | None = None
    registrant_identity_source: str | None = None


@dataclass(slots=True)
class ParseDiagnostics:
    toc_like_region_found: bool = False
    itemless_toc_fallback_used: bool = False
    internal_links_inspected: int = 0
    item_links_classified: int = 0
    valid_anchor_targets: int = 0
    native_outline_entries_detected: int = 0
    native_outline_entries_extracted: int = 0
    native_outline_entries_skipped: list[dict[str, str]] = field(default_factory=list)
    recovered_anchor_targets: list[dict[str, str]] = field(default_factory=list)
    unresolved_anchor_ids: list[str] = field(default_factory=list)
    unresolved_section_ids: list[str] = field(default_factory=list)
    non_forward_anchor_ids: list[str] = field(default_factory=list)
    ambiguous_item_classifications: list[str] = field(default_factory=list)
    caption_based_part_assignments: list[str] = field(default_factory=list)
    unmapped_reference_sections: list[str] = field(default_factory=list)

    @property
    def native_outline_coverage(self) -> float:
        if self.native_outline_entries_detected == 0:
            return 0.0
        return (
            self.native_outline_entries_extracted
            / self.native_outline_entries_detected
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "toc_like_region_found": self.toc_like_region_found,
            "itemless_toc_fallback_used": self.itemless_toc_fallback_used,
            "internal_links_inspected": self.internal_links_inspected,
            "item_links_classified": self.item_links_classified,
            "valid_anchor_targets": self.valid_anchor_targets,
            "native_outline_entries_detected": self.native_outline_entries_detected,
            "native_outline_entries_extracted": self.native_outline_entries_extracted,
            "native_outline_entries_skipped": self.native_outline_entries_skipped,
            "native_outline_coverage": self.native_outline_coverage,
            "recovered_anchor_targets": self.recovered_anchor_targets,
            "unresolved_anchor_ids": self.unresolved_anchor_ids,
            "unresolved_section_ids": self.unresolved_section_ids,
            "non_forward_anchor_ids": self.non_forward_anchor_ids,
            "ambiguous_item_classifications": self.ambiguous_item_classifications,
            "caption_based_part_assignments": self.caption_based_part_assignments,
            "unmapped_reference_sections": self.unmapped_reference_sections,
        }


@dataclass(slots=True)
class FilingParseResult:
    file: Path
    form_type: str
    status: ParseStatus
    sections: list[ExtractedSection] = field(default_factory=list)
    recognized_toc_entries: list[RecognizedTocEntry] = field(default_factory=list)
    diagnostics: ParseDiagnostics = field(default_factory=ParseDiagnostics)
    warnings: list[str] = field(default_factory=list)
    failure_reason: FailureReason | None = None
    details: str | None = None

    @property
    def sections_found(self) -> int:
        return len(self.sections)

    @property
    def canonical_sections_mapped(self) -> int:
        return sum(
            section.canonical_section_id is not None for section in self.sections
        )

    @property
    def semantic_only_sections(self) -> int:
        return sum(
            section.canonical_section_id is None and bool(section.semantic_categories)
            for section in self.sections
        )

    @property
    def unmapped_sections(self) -> int:
        return sum(
            section.canonical_section_id is None and not section.semantic_categories
            for section in self.sections
        )

    @property
    def mapping_status(self) -> MappingStatus:
        if not self.sections or self.canonical_sections_mapped == 0:
            return MappingStatus.NONE
        if self.canonical_sections_mapped == len(self.sections):
            return MappingStatus.COMPLETE
        return MappingStatus.PARTIAL
