# finbot-filings Roadmap

## Purpose

This roadmap records the intended evolution of `finbot-filings` from reliable
SEC acquisition and source extraction into a richer, feature-neutral filing
content layer. It is written to preserve the reasoning behind each milestone so
future implementation sessions do not have to reconstruct the package boundary
from individual parser decisions.

The primary downstream consumer is an LLM-assisted feature calculator. That
consumer should be able to inspect a compact filing inventory, choose relevant
narrative sections or filer-defined financial statements, retrieve exact source
facts, and then calculate features with complete provenance. It should not need
to read an entire 10-K, traverse raw XBRL linkbase XML, or infer values from
flattened HTML tables when structured facts are available.

## Governing principles

1. **Completeness and correctness come before cost reduction.** Deterministic
   extraction is preferred when it is reliable, but difficult cases may be
   escalated downstream to a targeted LLM or human review rather than silently
   omitted.
2. **Preserve source evidence before interpreting it.** Native section titles,
   filer-defined XBRL roles, concepts, relationships, contexts, and values are
   durable source artifacts. Canonical classifications are optional annotations,
   not extraction gates.
3. **Use the best source for each job.** HTML sections support narrative analysis.
   XBRL supports exact numerical retrieval. A downstream feature may use both.
4. **Do not force early chunking or semantic choices.** This package stops at
   native document sections and source-shaped XBRL structures. Model-specific
   splitting, token budgets, concept selection, and formulas belong downstream.
5. **Make uncertainty explicit.** Missing relationships, unresolved taxonomy
   imports, ambiguous roles, duplicate facts, and incomplete coverage must be
   reported in metadata rather than hidden by a best guess.
6. **Design for filing variation and taxonomy change.** Discovery must be based
   on XML structure and namespaces, not only filenames or one taxonomy year.
   Unknown roles and concepts must be retained.
7. **Keep every derived result auditable.** Outputs must retain accession,
   source paths and hashes, taxonomy namespaces and versions, parser/schema
   versions, and the method behind any derived annotation.

## Package boundary

`finbot-filings` describes **what a filing contains and how the filer organized
it**. A downstream feature package decides **what that information means for a
particular feature**.

This package owns:

- official SEC public-data acquisition and durable raw-source provenance;
- document-native section boundaries and titles;
- complete XBRL fact, context, unit, and dimension inventory;
- source labels and taxonomy concept metadata;
- filer-defined presentation, definition, and calculation relationships;
- feature-neutral statement inventories and ordered concept views;
- coverage, integrity, ambiguity, and fallback diagnostics.

The downstream feature/calculator package owns:

- definitions of ratios, signals, and research features;
- cross-filer semantic mappings such as the preferred revenue concept;
- final period, context, unit, dimension, and duplicate-fact selection policy;
- arithmetic and feature-specific validation thresholds;
- prompts, model selection, retries, and LLM/human escalation;
- recording the sections, statement roles, concepts, facts, and ranges used for
  each computed feature.

`finbot-filings` may report that a concept participates in a filer-defined role
named `Consolidated Balance Sheets`. It must not claim that the concept is the
universally correct input for a feature such as current ratio.

## Current foundation

The following capabilities are implemented:

- official ticker-to-CIK and submissions-based filing discovery, including SEC
  historical submission files;
- unified acquisition of primary HTML and available SEC XBRL packages/instances;
- raw storage under `TICKER/FORM/ACCESSION` with checksums and SEC provenance;
- deterministic native-outline section extraction without additional chunking;
- optional canonical and semantic section annotations that do not gate source
  extraction;
- complete normalization of SEC-generated instance facts into Parquet while
  preserving values, contexts, units, dimensions, document order, and duplicates;
- secure, no-network taxonomy inventory with content-based discovery, standalone
  and embedded linkbase support, source-aware freshness, external-dependency and
  local-resolution diagnostics, and success/failure manifests;
- complete, versioned Parquet materialization of filed label resources,
  presentation roles, and presentation relationships with exact source
  attributes, authoritative local endpoint resolution, external endpoint
  identity, coverage diagnostics, and source-aware freshness;
- human-readable inspection and export of normalized facts.

The current XBRL outputs are intentionally not reconstructed financial
statements. They preserve facts plus the filer label and presentation graphs,
but do not yet provide official standard-concept metadata, flattened statement
line order, dimensional networks, calculation networks, or feature-specific
fact selection.

## Corpus evidence guiding the roadmap

An offline inventory of the current 113-package development corpus found:

- all 113 packages contain filing-specific schemas plus label, presentation,
  definition, and calculation relationships;
- 107 packages use conventional standalone linkbase files;
- 6 Microsoft packages embed all linkbases inside the filing XSD;
- labels and presentation membership cover every fact concept outside the
  observed SEC cybersecurity and insider-trading disclosure namespaces;
- every package exposes role-name evidence for balance sheet, income/operations/
  loss, and cash-flow statements;
- every filing schema imports official standard taxonomy resources, and none of
  the 1,148 imported schemas are embedded in the ZIP;
- the corpus spans US-GAAP taxonomy versions 2023 through 2026;
- only about 16.5% of per-filing fact concepts are defined by the local filer
  schema, so complete datatype, period, and balance metadata requires exact,
  versioned official taxonomy resources.

These observations justified the implemented label and presentation datasets,
while a controlled taxonomy cache remains necessary for complete
standard-concept metadata. They also prove that linkbase discovery cannot rely
only on `_lab.xml`, `_pre.xml`, `_def.xml`, and `_cal.xml` filenames.

## Output conventions for future milestones

New filing-level outputs should remain under the existing
`XBRL_FOLDER/TICKER/FORM/ACCESSION` hierarchy. Durable tabular datasets should
use Parquet plus versioned sidecar metadata JSON, stable ordering, source hashes,
and additive schema evolution where practical. Raw files and `facts.parquet`
should not be rewritten merely to add a new enrichment table. Schema or path
changes require README, tests, and metadata updates, and the shared Finbot
catalog should be refreshed after material datasets are produced.

## Milestone 3: Versioned official taxonomy cache and concept metadata

**Status:** Planned

**Why:** Filing ZIPs define filer extensions but reference standard US-GAAP, SEC,
and XBRL schemas externally. Without the exact imported taxonomy versions, most
standard concepts lack authoritative identity, datatype, period type, balance,
and substitution-group metadata. This resolution must precede fact-linked
statement views rather than relying on conventions embedded in fragment IDs.

Add controlled retrieval and caching of exact imported resources from official
FASB, SEC, and XBRL.org URLs. The cache must:

- resolve the URI referenced by the filing rather than substituting the newest
  taxonomy;
- preserve source URL, namespace, taxonomy version, retrieval time, and hash;
- support offline reuse and deterministic reprocessing;
- validate XML securely and detect changed content for an existing URI;
- retain unknown namespaces instead of rejecting future taxonomies.

Then produce `concepts.parquet` with QName, namespace, local name, source schema,
standard-versus-extension status, datatype, period type, balance, abstract/nillable
flags, and other source metadata where available.

Completion requires tests across every taxonomy version represented in the
corpus and an explicit unresolved-metadata state when an official dependency is
unavailable.

## Milestone 4: Statement inventory and ordered concept views

**Status:** Planned

**Why:** The downstream calculator should select logical resources such as
`Consolidated Balance Sheets`, not physical XML files or raw graph edges. It also
should not have to implement generic XBRL graph traversal before it can search
for feature inputs.

Derive feature-neutral views such as:

- `statement_index.parquet` — role URI, exact role definition, root concepts,
  concept count, relationship count, and available fact/period summary;
- `statement_lines.parquet` — role URI, concept QName, depth, deterministic
  traversal order, source relationship order, display label, and whether facts
  exist for the concept;
- an accession-level content index combining native section descriptors and
  XBRL role descriptors for discovery by downstream agents.

The ordered view may attach all candidate facts or summarize their availability,
but it must not choose the final feature value. Preserve the original graph so a
flattened traversal can always be audited.

Completion requires deterministic traversal, cycle and multi-parent diagnostics,
no loss of source relationships, and checks against rendered primary statements
in a representative filing corpus.

## Milestone 5: Definition and dimensional relationships

**Status:** Planned

**Why:** Correct feature values depend on knowing whether a fact is consolidated
or belongs to a segment, geography, product, legal entity, or other dimensional
slice. Labels alone cannot prevent a calculator from selecting a valid but wrong
dimensional fact.

Materialize definition roles and relationships, including:

- fact/primary-item to hypercube relationships;
- hypercube to axis;
- axis to domain;
- domain to member;
- default members;
- closed/context-element and other relevant source attributes.

Retain typed dimensions and unknown arcroles without forcing them into an
explicit-member model. Provide graph and coverage diagnostics, but leave
feature-specific consolidated-context selection to the downstream package.

## Milestone 6: Calculation relationships

**Status:** Planned

**Why:** Calculation networks help explain subtotal composition and validate
candidate values, but they are not complete enough to define financial features
or guarantee that every rendered total will reconcile.

Materialize calculation roles and weighted parent-child edges for both legacy
summation-item and Calculation 1.1 relationships. Preserve network role, order,
weight, arcrole, and provenance.

Expose validation evidence without silently replacing filed values, inferring
missing facts, or treating a calculation network as a feature formula. Feature
computation and tolerance policy remain downstream.

## Milestone 7: Downstream retrieval contract

**Status:** Planned

**Why:** An LLM feature calculator is more reliable when it receives a compact,
typed inventory and narrowly retrieved evidence instead of whole filings or raw
taxonomy graphs.

Define stable read/query interfaces that allow a downstream consumer to:

- list native sections and XBRL roles for one accession;
- retrieve selected section text by source section ID;
- retrieve an ordered filer-defined statement by role URI;
- search concepts by QName and labels;
- retrieve all candidate facts with contexts, units, dimensions, and provenance;
- inspect coverage and ambiguity warnings before choosing an input.

The interface should return logical identifiers and source evidence. It must not
contain ratio definitions, research-specific concept mappings, prompts, or model
configuration.

## Milestone 8: Quality gates and fallback signals

**Status:** Planned

**Why:** Robustness means knowing when deterministic evidence is incomplete, not
merely succeeding on familiar filers. Downstream automation needs explicit cues
for when to use alternate evidence or request semantic review.

Add corpus-level and filing-level diagnostics for:

- label, relationship, role, endpoint, and fact coverage;
- unresolved taxonomy imports or locator targets;
- graph cycles, duplicate arcs, multiple roots, and disconnected concepts;
- statement roles with no facts or facts with no presentation membership;
- dimensional and calculation-network completeness;
- taxonomy versions and previously unseen namespaces/arcroles;
- source changes that invalidate derived outputs.

Suggested downstream fallback order is:

1. ordered XBRL statement and exact facts;
2. direct fact search with complete context metadata;
3. relevant native HTML section text;
4. targeted LLM interpretation of narrowed evidence;
5. human review.

The fallback decision and any LLM call belong downstream, but this package must
provide enough diagnostics and provenance to make that decision safely.

## Deferred and explicitly out of scope

The following do not belong in this roadmap unless package boundaries are
deliberately revisited:

- universal revenue, earnings, or balance-sheet concept selection;
- ratios, factor scores, labels, rankings, or trading signals;
- model-specific section splitting and token budgeting;
- prompts, agents, model APIs, retries, or LLM cost management;
- silently generated facts, semantic ownership, or parent/subsidiary defaults;
- portfolio construction, backtesting, inference, or execution.

See [README.md](README.md) for current commands,
[Native Section Architecture](docs/native-section-architecture.md) for the
section contract, and
[Complex Filing Layouts and Recovery Registry](docs/parser-limitations.md) for
known source-extraction edge cases.
