# Future changes and investigations

Track unscheduled ideas here, with a stable ID, motivation, revisit trigger and
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

- Status: Observed during initial production activation; investigation pending.
- Evidence: Citigroup 10-K artifact
  `0000831001-26-000011/0000831001-26-000011.txt` exceeded the configured 64-MiB cap
  at `2026-10-10T14:31:48.814400Z`. Its terminal acquisition/dead-letter checkpoints,
  logs, metric and one visible failed-work queue message agree. See DEPLOYMENT.md.
- Current contract: HLD section 8 explicitly bounds v0 acquisition to 64 MiB per
  artifact; larger documents fail. The task stayed healthy and continued other work.
- Investigation: Establish the document size and desired completeness policy;
  assess bounded streaming/spooling and memory constraints before any cap increase.
  Define a reviewed redrive procedure if supported acquisition behavior changes.
- Completion: Record the approved policy/design and validate any new acquisition
  behavior before redriving the durable failed artifact. Do not delete terminal
  records or silently omit the original submission file.

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
