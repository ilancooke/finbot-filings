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
its input section IDs and ranges, model, prompt version, method, and review status.

## Active source-extraction cases

### Celestica trailing financial-statement appendix

- **Ticker:** `CLS`
- **Accession:** `0001030894-25-000014`
- **Native outline:** Complete — 23 linked Items
- **Section assignment:** Incomplete

Item 8 contains a linked index of financial statements, while the actual F-pages
appear after Item 16 and the signature material without another top-level TOC
boundary. The text is retained but is currently absorbed into Item 16. This is a
separate noncontiguous/trailing-material problem; 100% linked-outline coverage
does not prove that an unlinked appendix is assigned to its semantic owner.

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

### American Airlines combined Form 10-Q

- **Ticker:** `AAL`
- **Accessions:** `0000006201-25-000052`, `0000006201-26-000032`, `0000006201-26-000052`
- **Source extraction:** Resolved — 9 native sections each
- **Canonical mapping:** Partial — 7 exact and 2 semantic-only sections each

Each filing uses Part I Item 1A for American Airlines Group Inc. financial
statements and Part I Item 1B for American Airlines, Inc. financial statements.
The native parser reads unlinked Part markers from the selected TOC table and
retains both structurally valid noncanonical Items. Their direct anchors define
exact boundaries, their titles supply registrant-name evidence, and both receive
the `financial_statements` semantic category without being forced into canonical
Part I Item 1.

Other resolved layout variations enforced by tests include:

- conflicting numeric-only TOC links, as seen in AMD;
- filing-body cross-references mistaken for TOC links, as seen in Walmart and Exxon;
- split Item suffixes and fragmented link text, as seen in Tesla and JPM;
- split TOC rows whose Item fragment points elsewhere while the title/page links
  reach the correct destination, including destination headings prefixed by
  `Table of Contents`, as seen in Nike and Tesla;
- broken redundant link fragments when a sibling link in the same row still
  resolves the section; the broken IDs remain recorded in diagnostics;
- explanatory TOC prose that mentions an Item, as seen in Nike;
- Item-less but strongly validated TOCs, as seen in Johnson & Johnson;
- two-column topic outlines with multiple independent links per row, as seen in JPM.

### AMC broken Item 1A TOC targets

- **Ticker:** `AMC`
- **Accessions:** `0001411579-25-000073`, `0001411579-26-000059`
- **Source extraction:** Resolved — 11 native sections each
- **Canonical mapping:** Complete

Both filings link the Item 1A TOC row to `#Item1ARiskFactors`, but omit that ID
from the document body. Each body contains one exact `Item 1A. Risk Factors`
heading immediately preceded by a unique opaque empty anchor. The constrained
missing-target recovery uses that anchor and records the broken ID, recovered ID,
section ID, and recovery method in `recovered_anchor_targets` diagnostics. It
does not activate when headings are non-exact, lack an adjacent anchor, or produce
more than one candidate.

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

As of 2026-08-22, all 113 filings in the downloaded development corpus complete
source extraction with 100% native-outline coverage, totaling 1,896 native
sections. The outputs contain 1,654 exact canonical mappings, 59 semantic-only
annotations, and 183 unmapped
sections. These annotation counts are intentionally reported separately from
source-extraction success. This small corpus is a regression sample, not an
estimate of performance across all SEC filings.
