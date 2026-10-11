# Future changes and investigations

Track future ideas and their disposition here, with a stable ID, motivation, revisit trigger and
links to affected decisions. An entry is not implementation authorization or a
commitment to a particular solution. Accepted decisions belong in `docs/adr/`;
scheduled implementation belongs in MIGRATION_PLAN and the relevant phase plan.
Keep this backlog linked to those documents rather than duplicating their contracts.

## ARCH-001 — Reduce recovery delay during active earnings windows

- Status: Deferred beyond v0; revisit using production recovery measurements.
- Current decision: [ADR 008](adr/008-use-conservative-ecs-automatic-recovery.md).
- Problem: The accepted 150-second startup quiet period alone creates a 2.5-minute
  polling interruption. Total recovery can be longer. During expected earnings,
  this is substantial relative to normal seconds-scale polling and the service's
  quick-ingestion objective. Recovery restores durable progress, not timeliness.
- Trigger: Recovery measurements show unacceptable discovery delay during active
  windows, overlapping SEC activity is observed, a strict cross-process request
  ceiling is required, or the design needs multiple tasks.
- Investigation: Compare verified faster task handoff, shared ownership with
  fencing, and centralized SEC request dispatch. Assess paused/stale processes,
  lost acknowledgments and control-plane failures; a lease alone must not be
  assumed to prevent stale request dispatch. Preserve the aggregate SEC budget.
- Evidence needed: Failure-to-first-poll timing split into detection, shutdown,
  provisioning, quiet period and initialization; discovery delay for filings
  accepted during the outage; observed task/request overlap; recovery reliability,
  complexity and cost of alternatives.
- Completion: Record a new or superseding ADR, update HLD/LLD and schedule the
  approved work. Avoid promising uninterrupted earnings ingestion before validation.

## DATA-001 — Review oversized SEC submission acquisition

- Status: Root cause identified; bundle filtering implemented in
  [Phase 9](PHASE_9_PLAN.md), awaiting production rollout/validation. Review genuine
  oversized selected documents separately.
- Evidence: Citigroup 10-K artifact
  `0000831001-26-000011/0000831001-26-000011.txt` exceeded the configured 64-MiB cap
  at `2026-10-10T14:31:48.814400Z`. Its terminal acquisition/dead-letter checkpoints,
  logs, metric and one visible failed-work queue message agree. See DEPLOYMENT.md.
  Subsequent investigation found three additional oversized Alcoa submission texts:
  `0001193125-26-388649.txt` (8-K), `0000950170-24-018069.txt` and
  `0000950170-25-024242.txt` (10-K). Four terminal records/metric samples agree with
  four visible failed-work messages at inspection. They span application revisions
  5 and 6; the seven-minute alarm episodes reflect sparse metric evaluation and
  do not prove redrive or repair when the alarm returns to OK.
- Current contract: HLD section 8 explicitly bounds v0 acquisition to 64 MiB per
  artifact; larger documents fail. The task stayed healthy and continued other work.
- Agreed resolution: [ADR 012](adr/012-acquire-earnings-releases-and-financial-reports-selectively.md)
  excludes complete submission TXT bundles from new and recovered pending work.
  Phase 9 preserves old terminal facts and messages; it does not redrive these bundles.
- Remaining investigation: Assess bounded streaming/spooling and memory constraints
  if a selected primary/release/supplement exceeds the unchanged acquisition cap.
  The current configuration and downloader both enforce 64 MiB as a maximum;
  an environment-variable increase alone is rejected. Determine actual remote
  sizes and whether rejection came from Content-Length or counted response bytes.
  Define a reviewed redrive procedure if supported acquisition behavior changes.
- Completion: Deliver the agreed bundle exclusion while preserving original
  terminal evidence. Any future support/redrive for genuinely oversized selected
  documents requires a separate reviewed change.

## DATA-002 — Define earnings-focused filing and document selection

- Status: Filtering implemented in [Phase 9](PHASE_9_PLAN.md), production rollout
  pending. Deployed behavior remains unchanged until delivery.
- Accepted direction: Historical backfill must be separate from live ingestion;
  see [ADR 011](adr/011-separate-live-ingestion-from-historical-backfill.md).
- User preferences: Focus acquisition on earnings-related information. Avoid
  standalone logos/graphics. Exclude presentation decks, including earnings decks;
  this is not a
  claim that their content always duplicates releases or periodic reports.
- Current behavior: All supported `filings.recent` rows enter discovery without
  an age cutoff. Item 2.02 evidence affects earnings-polling satisfaction only.
  Package enumeration acquires almost every directory file, including the complete
  submission TXT, images and data files. These remain the deployed contracts.
- Accepted filing policy ([ADR 012](adr/012-acquire-earnings-releases-and-financial-reports-selectively.md)):
  Select 8-K/8-K/A earnings candidates using unambiguous Item
  2.02 metadata, including a deliberate amendment policy. Missing or ambiguous
  evidence needs a visible review path; it must not be interpreted as proof of
  irrelevance. Retain 10-Q/10-K primary reports and their amendments as financial
  statement sources, subject to the live boundary or an explicit backfill scope.
- Accepted artifact policy: Retain the primary filing for provenance and exhibit
  routing. For selected 8-Ks acquire earnings releases and financial supplements
  in HTML/text/PDF form, identified through descriptions and primary-document
  exhibit references rather than a fixed `99.1` rule. Exclude presentation decks,
  standalone graphics/logos, complete submission TXT bundles, unrelated exhibits
  and auxiliary directory files from routine acquisition. Image-only financial
  content and ambiguous exhibit descriptions require a recorded diagnostic;
  skipping graphics must not be represented as complete content preservation.
- Evidence: [KDP's August 6, 2026 8-K](https://www.sec.gov/Archives/edgar/data/1418135/000141813526000048/kdp-20260806.htm)
  points to Exhibit 99.1 for its earnings results. Its
  [package listing](https://www.sec.gov/Archives/edgar/data/1418135/000141813526000048/0001418135-26-000048-index.htm)
  lists a 26,544-byte primary report, a 1,871,545-byte release, two 943,105-byte
  graphics and a 5,623,267-byte complete submission TXT. These are SEC-listed
  sizes, not newly downloaded byte counts.
  [CNA's August 3, 2026 8-K](https://www.sec.gov/Archives/edgar/data/21175/000002117526000043/cna-20260803.htm)
  assigns release/supplement/presentation/remarks to Exhibits 99.1/99.2/99.3/99.4;
  dropping every exhibit except 99.1 would omit the financial supplement.
  [EPR's earnings presentation](https://www.sec.gov/Archives/edgar/data/1045450/000104545026000022/q12026earningscallpresen.htm)
  illustrates slides distributed as images and includes guidance/KPIs; deck
  exclusion intentionally narrows coverage.
- Implementation concerns: Apply small deterministic selectors to discovery and
  recovered pending work. Use existing checkpoints and structured logs for policy
  and skip/unresolved reasons. Skips leave pending indexes without download alarms
  or changes to stored bytes, publication history or terminal evidence.
- Completion: Deliver Phase 9 filtering and the configured live cutoff. Validate
  releases, supplements, decks, inline/image-only results, amendments, ambiguous
  descriptions and legacy pending-work checks offline. Retain the 64-MiB bound.

## DATA-003 — Add a deliberate historical backfill operation

- Status: Deferred beyond the focused Phase 9 filtering change.
- Decision: [ADR 011](adr/011-separate-live-ingestion-from-historical-backfill.md).
- Scope: A supported operator command with explicit company/time bounds, using
  the same selectors, artifact identities and durable acquisition path. Review
  SEC history-file traversal, finite budgets, resume behavior and request-budget
  coordination when this operation is needed.
- Trigger: The user requests historical coverage or recovery of a gap beyond the
  recent submissions response. Do not widen/reset the live cutoff as a substitute.
- Completion: Plan, implement and validate that explicit operation separately;
  no backfill framework is required to deliver the current filters.

## OPS-001 — Distinguish historical catch-up from live discovery latency

- Status: Observed during initial production activation; investigation pending.
- Evidence: DiscoveryLatencyMs alarm fired during initial catch-up on 2026-10-10.
  A verified example was accepted August 7 and first discovered during startup;
  acceptance-to-discovery age exceeds the 60-second threshold by design.
- Investigation: Assess separate metrics/labels or alarm applicability for initial
  historical collection versus newly accepted filings. Preserve the observed
  acceptance and discovery timestamps; do not relabel old filings as timely.
- Completion: Review the intended timeliness contract and apply a tested metric or
  alarm change through source-controlled infrastructure/application delivery.
  No alarm suppression is authorized by this backlog entry.
