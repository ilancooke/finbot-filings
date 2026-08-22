"""Deterministic section-local chunking for downstream model inputs."""

from __future__ import annotations

from finbot_filings.parsing.models import ExtractedSection, SectionChunk

DEFAULT_MAX_CHARS = 24_000
DEFAULT_OVERLAP_CHARS = 1_000


def _word_boundary(value: str, position: int, *, search_backward: bool) -> int:
    if position <= 0 or position >= len(value):
        return max(0, min(position, len(value)))
    if search_backward:
        boundary = value.rfind(" ", 0, position)
        return boundary if boundary > 0 else position
    boundary = value.find(" ", position)
    return boundary if boundary >= 0 else len(value)


def chunk_section(
    section: ExtractedSection,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[SectionChunk]:
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be non-negative and smaller than max_chars")
    if len(section.text) <= max_chars:
        return []

    chunks: list[SectionChunk] = []
    start = 0
    while start < len(section.text):
        proposed_end = min(start + max_chars, len(section.text))
        end = _word_boundary(section.text, proposed_end, search_backward=True)
        if end <= start:
            end = proposed_end
        chunk_start = start
        while chunk_start < end and section.text[chunk_start].isspace():
            chunk_start += 1
        chunk_end = end
        while chunk_end > chunk_start and section.text[chunk_end - 1].isspace():
            chunk_end -= 1
        text = section.text[chunk_start:chunk_end]
        chunk_order = len(chunks) + 1
        chunks.append(
            SectionChunk(
                chunk_id=f"{section.source_section_id}_chunk_{chunk_order:03d}",
                source_section_id=section.source_section_id,
                chunk_order=chunk_order,
                start_character=chunk_start,
                end_character=chunk_end,
                character_count=len(text),
                estimated_tokens=max(1, (len(text) + 3) // 4),
                text=text,
            )
        )
        if end >= len(section.text):
            break
        proposed_start = max(0, chunk_end - overlap_chars)
        next_start = _word_boundary(
            section.text, proposed_start, search_backward=False
        )
        start = next_start if next_start < chunk_end else chunk_end
    return chunks


def chunk_sections(
    sections: list[ExtractedSection],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[SectionChunk]:
    return [
        chunk
        for section in sections
        for chunk in chunk_section(
            section, max_chars=max_chars, overlap_chars=overlap_chars
        )
    ]
