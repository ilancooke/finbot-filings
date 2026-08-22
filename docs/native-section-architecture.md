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
  -> manifest plus text files
  -> downstream feature/agent routing
  -> optional model-specific splitting
  -> targeted LLM calls when required
```

Downloading and parsing remain separate commands so either stage can be retried,
audited, or scaled independently. A production orchestrator may run them back to
back for one filing, but the persisted raw HTML and derived manifest remain the
boundary between stages.

Raw filings and derived sections share the hierarchy
`TICKER/FORM/ACCESSION`. The form directory is always the canonical exact form
name (`10-K` or `10-Q`), keeping annual and quarterly filings independently
browsable without changing the configured storage roots.

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

## Deferred splitting

This package stops at complete native sections. It does not split large sections
into smaller chunks. A downstream feature or model-input workflow can make that
decision using the selected model's tokenizer, context limit, feature objective,
and retrieval strategy. That later workflow should retain the source section ID
and any selected character or token ranges as provenance.

## Responsibility boundary

`finbot-filings` owns source acquisition, deterministic boundaries, provenance,
and conservative annotations. A downstream feature package or agent owns:

- deciding which sections are relevant to a feature;
- splitting a selected section when required by a model or feature;
- assigning parent/subsidiary roles or ticker-specific defaults;
- prompts, model choice, retries, and structured feature validation;
- recording selected section IDs and ranges, model, and prompt version;
- escalating genuinely semantic recovery to an LLM or human review.

This keeps parsing reproducible while allowing feature-specific routing to evolve
without redownloading or reparsing filings.
