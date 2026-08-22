# Native Section Architecture

## Purpose

The durable parsing artifact is the filing's own top-level outline, not a forced
23-section 10-K or 11-section 10-Q template. This preserves the source structure
needed for later feature generation and avoids discarding useful nonstandard
sections.

## Workflow

```text
SEC primary HTML
  -> deterministic native-section extraction
  -> optional canonical and semantic annotations
  -> deterministic section-local chunks
  -> manifest plus text files
  -> downstream feature/agent routing
  -> targeted LLM calls when required
```

Downloading and parsing remain separate commands so either stage can be retried,
audited, or scaled independently. A production orchestrator may run them back to
back for one filing, but the persisted raw HTML and derived manifest remain the
boundary between stages.

## Source-section contract

Each section records:

- a stable `source_section_id` derived from the native Item/Part or title;
- the exact normalized `source_title` shown by the filing outline;
- its internal anchor and physical document order;
- normalized section text and source-file provenance;
- an optional `canonical_section_id` with a deterministic mapping method;
- zero or more conservative `semantic_categories`;
- a registrant name only when the section caption explicitly supplies it.

AAL Items 8A and 8B therefore remain two independent source sections. Their
captions provide registrant-name evidence, but this package does not infer
`registrant_role` or choose a ticker-default section.

## Chunk contract

Whole section files are always retained. A section larger than the configured
maximum receives deterministic word-boundary chunks with configurable overlap.
Each chunk has a stable ID, section-local order, character offsets, estimated
token count, and a reference to exactly one `source_section_id`. Sections are
never mixed within a chunk.

## Responsibility boundary

`finbot-filings` owns source acquisition, deterministic boundaries, provenance,
and conservative annotations. A downstream feature package or agent owns:

- deciding which sections or chunks are relevant to a feature;
- assigning parent/subsidiary roles or ticker-specific defaults;
- prompts, model choice, retries, and structured feature validation;
- recording the selected section/chunk IDs, model, and prompt version;
- escalating genuinely semantic recovery to an LLM or human review.

This keeps parsing reproducible while allowing feature-specific routing to evolve
without redownloading or reparsing filings.
