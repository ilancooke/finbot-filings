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
