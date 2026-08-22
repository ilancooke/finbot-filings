# Complex Filing Layouts and Recovery Registry

This registry records filing layouts that require materially complex treatment.
The native-section contract resolves many former canonical-Item failures by
preserving what the filing actually exposes instead of forcing every filing into
one fixed SEC Item inventory.

## When a case belongs here

Add a case when source boundaries or ownership remain genuinely ambiguous, a
recovery requires assembling noncontiguous fragments, or a deterministic rule
could silently corrupt otherwise valid filings. Ordinary markup variations
belong in regression tests once resolved.

## Recovery principles

Recovery proceeds in explicit tiers:

1. **Native deterministic parser** — Preserve the filing's primary linked outline.
2. **Format-specific deterministic adapter** — Add a tested adapter when source boundaries can be proved.
3. **Targeted LLM recovery** — Send narrowed candidate fragments for one task, never an entire 10-K by default.
4. **Human review** — Require review when identity, ownership, boundaries, or ordering remain uncertain.

The parser does not silently invoke an LLM. Any downstream recovery should record
its input section or chunk IDs, model, prompt version, method, and review status.

## Active source-extraction cases

There are no active failures in the current sample corpus. This is not evidence
that every SEC filing layout is supported; new failures should be added here only
after their structure has been inspected.

## Deferred canonical reconstruction

### Intel 2025 Form 10-K

- **Ticker:** `INTC`
- **Accession:** `0000050863-26-000011`
- **Source extraction:** Resolved — 24 topic-oriented native sections
- **Canonical mapping:** None
- **Status:** Native output implemented; canonical reconstruction deferred

Intel presents a topic-oriented outline and a later Form 10-K Cross-Reference
Index. The native parser preserves topics including Overview, Risk Factors,
Liquidity and Capital Resources, Auditor's Reports, Consolidated Financial
Statements, Notes, Controls, and Exhibits.

Reconstructing traditional SEC Items would be a different operation: cross-reference
rows can point backward, span page ranges, omit direct Item boundaries, or map one
Item to multiple topics. If a downstream feature specifically requires canonical
Item reconstruction, it should assemble only the relevant native fragments and
record recovered provenance. The absence of canonical mapping does not make the
source extraction incomplete.

## Resolved cases

### American Airlines combined Form 10-K

- **Ticker:** `AAL`
- **Accession:** `0000006201-26-000014`
- **Source extraction:** Resolved — 24 native sections
- **Canonical mapping:** Partial

The filing has no conventional Item 8. It exposes two independent sections:

- Item 8A — Consolidated Financial Statements and Supplementary Data of American Airlines Group Inc.
- Item 8B — Consolidated Financial Statements and Supplementary Data of American Airlines, Inc.

Both are preserved with their native IDs and caption-derived registrant names.
Neither is falsely aliased to canonical Item 8, combined, assigned a registrant
role, or designated as the ticker default.

Other resolved layout variations enforced by tests include:

- conflicting numeric-only TOC links, as seen in AMD;
- filing-body cross-references mistaken for TOC links, as seen in Walmart and Exxon;
- split Item suffixes and fragmented link text, as seen in Tesla and JPM;
- explanatory TOC prose that mentions an Item, as seen in Nike;
- Item-less but strongly validated TOCs, as seen in Johnson & Johnson;
- two-column topic outlines with multiple independent links per row, as seen in JPM.

## Case template

```markdown
### Company and filing

- **Ticker:**
- **Accession:**
- **Source-extraction status:**
- **Canonical-mapping status:**
- **Complexity:** Low | Medium | High
- **Likely recovery tier:**
- **Status:** Investigating | Unresolved | Implemented | Rejected

Describe the filing structure and observed evidence.

Why a simple fix is unsafe:

- ...

Potential recovery path:

1. ...

Required provenance or review:

- ...
```

## Current corpus snapshot

As of 2026-08-21, the downloaded development corpus produces:

- 15 of 15 successful 10-Q source extractions, totaling 153 native sections.
- 29 of 29 successful 10-K source extractions, totaling 689 native sections.
- No partial or failed source extractions.

The 10-K native sections include 573 exact canonical mappings, 20 semantic-only
annotations, and 96 unmapped sections. These annotation counts are intentionally
reported separately from source-extraction success. This small corpus is a
regression sample, not an estimate of performance across all SEC filings.
