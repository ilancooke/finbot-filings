# ADR 012: Acquire earnings releases and financial reports selectively

- Status: Accepted; implemented in source, production rollout pending
- Date: 2026-10-10
- Supersedes: HLD sections 6.5–6.6 and the Phase 2/4 requirement to acquire all
  supported forms and almost every accession-directory file
- Complements: [ADR 011](011-separate-live-ingestion-from-historical-backfill.md)

## Context

Production acquisition encountered oversized complete submission TXT files. SEC
filing packages also contain transaction documents, presentations, logos and data
files that are outside the project's intended earnings inputs. Broad acquisition
uses the shared SEC budget and generates failures for documents we do not need.

KDP's August 6, 2026 earnings 8-K points to Exhibit 99.1 for the results. Its
primary report provides context and exhibit references. CNA's August 3, 2026
earnings 8-K identifies its financial supplement as 99.2 and presentation as 99.3:
exhibit numbers alone are not a reliable selection policy.

The user accepted earnings-focused filing selection, releases and supplements,
primary periodic reports, retention of the primary 8-K for provenance, and
exclusion of decks, standalone graphics and complete submission bundles.

## Decision

The versioned policy is `earnings-acquisition-v1`, used by live ingestion and
explicit backfill:

- Select 8-K and 8-K/A earnings candidates using unambiguous SEC Item 2.02
  evidence. A known item list without 2.02 is excluded. Missing, conflicting or
  ambiguous evidence requires a visible review outcome, not assumed irrelevance.
  Amendments retain their own accession identities; they do not satisfy the
  existing aggressive-polling satisfaction heuristic.
- Select 10-Q, 10-K, 10-Q/A and 10-K/A primary reports as financial statement
  sources, within the live boundary or requested backfill range.
- Retain the primary report of a selected filing. For selected 8-Ks also acquire
  earnings releases and financial supplements in HTML, text or PDF form.
  Determine roles from SEC document descriptions and primary-document exhibit
  references; do not hardcode `99.1` as the only eligible exhibit.
- Exclude presentation decks, standalone images/logos, complete submission TXT
  bundles, unrelated exhibits and auxiliary directory files. Do not automatically
  follow image, stylesheet, script or external investor-relations links.
- A complete submission TXT is excluded by its accession-bundle identity, not
  by banning every `.txt` document. A legitimate selected text release is allowed.
- Unresolved exhibit roles or image-only results produce a recorded diagnostic
  in existing checkpoints and structured logs. The acquisition service routes documents structurally; it does not
  extract financial values or use an LLM to decide relevance.
- Record the policy version and selection/skip/unresolved reasons using existing
  checkpoints and structured logs. No separate manifest store or review system is
  required. Completion covers selected documents; it does not mean that the full
  SEC package or every earnings disclosure was acquired.
- Excluded documents never become download work or terminal download failures.
  Genuine failures of selected documents retain the existing retry/dead-letter
  behavior and 64-MiB bound.
- Preserve original SEC bytes, accession/artifact identities, existing S3 objects,
  canonical discovery facts, publication timestamps and at-least-once events.
  Already published artifacts are not retracted when the selection policy changes.

## Consequences

The service prioritizes releases, supplements and periodic reports. Presentation
exclusion intentionally narrows coverage: decks may contain guidance and KPIs,
and HTML with image-only content cannot be treated as fully preserved without
its images. Raw HTML may refer to excluded graphics and need not render offline.

Missing evidence and unsupported layouts remain inspectable rather than causing
silent broad fallback or a false claim of complete earnings coverage.

Previously admitted pending acquisition/publication work passes the same filters
on recovery. Old failure records and failed-work messages are not deleted or
redriven as a side effect of selecting fewer documents. No bulk migration tool is
required. See [Phase 9](../PHASE_9_PLAN.md).

## Implementation status and references

The policy is implemented in source and validated offline; production rollout is
pending. The deployed image still follows broad acquisition until released.
[PHASE_9_PLAN.md](../PHASE_9_PLAN.md) records the focused implementation and checks.
Primary metadata/text inspection is bounded to 4 MiB; unresolved routing is logged.
Raw selected-document acquisition retains the existing 64-MiB limit.

- [KDP primary 8-K](https://www.sec.gov/Archives/edgar/data/1418135/000141813526000048/kdp-20260806.htm)
- [CNA primary 8-K](https://www.sec.gov/Archives/edgar/data/21175/000002117526000043/cna-20260803.htm)
- [EPR presentation example](https://www.sec.gov/Archives/edgar/data/1045450/000104545026000022/q12026earningscallpresen.htm)
