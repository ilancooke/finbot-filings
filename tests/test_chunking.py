from __future__ import annotations

import pytest

from finbot_filings.parsing.chunking import chunk_section
from finbot_filings.parsing.models import ExtractedSection


def section_with_text(text: str) -> ExtractedSection:
    return ExtractedSection(
        section_id="item7_mda",
        part=None,
        item="7",
        title="Management Discussion",
        anchor_id="mda",
        physical_order=1,
        text=text,
        source_section_id="item7",
        source_title="Management Discussion",
        canonical_section_id="item7_mda",
    )


def test_short_section_does_not_create_redundant_chunks() -> None:
    assert (
        chunk_section(
            section_with_text("short text"), max_chars=100, overlap_chars=10
        )
        == []
    )


def test_long_section_chunks_deterministically_within_its_source_section() -> None:
    section = section_with_text(" ".join(f"word{i}" for i in range(100)))

    first = chunk_section(section, max_chars=120, overlap_chars=20)
    second = chunk_section(section, max_chars=120, overlap_chars=20)

    assert first == second
    assert len(first) > 1
    assert [chunk.chunk_order for chunk in first] == list(range(1, len(first) + 1))
    assert all(chunk.source_section_id == "item7" for chunk in first)
    assert all(chunk.character_count <= 120 for chunk in first)
    assert all(
        section.text[chunk.start_character : chunk.end_character] == chunk.text
        for chunk in first
    )


@pytest.mark.parametrize(
    ("max_chars", "overlap_chars"),
    [(0, 0), (100, -1), (100, 100)],
)
def test_invalid_chunk_configuration_fails(max_chars: int, overlap_chars: int) -> None:
    with pytest.raises(ValueError):
        chunk_section(
            section_with_text("long enough"),
            max_chars=max_chars,
            overlap_chars=overlap_chars,
        )
