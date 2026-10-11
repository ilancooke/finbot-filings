# Open work

The migration and earnings-filtering delivery are complete. These items track
remaining investigations or future scope; they do not authorize implementation,
cloud changes, provider calls or terminal redrive. Current contracts belong in
[ARCHITECTURE](ARCHITECTURE.md), rationale in [DECISIONS](DECISIONS.md), and operator
procedures in [DEPLOYMENT](DEPLOYMENT.md). Retire completed plans after carrying
forward their durable contracts and unresolved findings.

## ARCH-001 — Recovery delay

Deferred beyond the current single-task design. [ADR 008](DECISIONS.md#adr-008)
accepts a 150-second SEC quiet period; detection/shutdown/provisioning add further
delay. Measure failure-to-first-poll components, discovery delay during active
earnings windows and any overlapping dispatch. Revisit if latency is unacceptable,
overlap appears, strict cross-process enforcement is required or multiple tasks
are needed. Compare verified handoff, fencing and centralized dispatch; a lease
alone does not prevent stale requests. Record a superseding decision and realistic
failure tests before changing recovery guarantees.

## DATA-001 — Oversized selected documents

Bundle exclusion is delivered. The initial checkpoint preserved four historical
submission TXT failures. The October 11 investigation found 14 pre-filter oversized
submission failures across six companies, with no new filtering-era failures.
The explicitly authorized ADR 013 acquisition reset later cleared their database
records and queue messages; this was deletion, not repair/redrive. Dated log history
remains available.

Investigate bounded streaming/spooling only if a genuinely selected
primary/release/supplement exceeds the unchanged 64-MiB limit. Measure remote size,
Content-Length versus counted-byte rejection and memory headroom. An environment
increase alone is rejected by configuration/downloader. Any supported behavior
change needs explicit redrive design and failure/restart validation.

## DATA-003 — Explicit historical backfill

Deferred until historical coverage is requested. [ADR 011](DECISIONS.md#adr-011)
requires explicit company/date scope, shared selectors/identities and the aggregate
SEC budget. Review historical submissions traversal, finite budgets, resume behavior
and coordination with live ingestion. Do not reset/widen the live cutoff as a
substitute. Current recent-response coverage does not promise recovery of long
outage gaps.

## OPS-001 — Historical versus live discovery latency

Initial catch-up triggered the acceptance-to-discovery alarm for older filings;
one verified example was accepted August 7 and discovered at October 10 startup.
Assess whether current fixed-cutoff behavior resolves the operational problem or
needs separate alarm applicability/metrics. Preserve original timestamps rather
than relabeling old work as timely. Close after review or deliver a tested metric/
alarm change. Alarm OK does not prove terminal work repaired; sparse counter
evaluation and failed-work queue state have different meanings.

## OPS-002 — Observe live selection and daily calendar operation

Filtering subsequently stored/published DAL's `0000027904-26-000038/dal-20260930.htm`
before the explicit acquisition reset. The new-layout restart is healthy and
polling, but no fresh download was observed in its immediate startup window.
Observe selected primary/release/supplement artifacts, exclusions, routing warnings
and new-layout S3/database/publication checkpoints as polling repopulates state.
Verify ongoing daily Yahoo scope/freshness, coverage gaps, provider
access arrangements, recovery reliability and sustained memory use. Local/cloud
collection success is not complete all-company coverage or long-term reliability.
Close observations on recorded evidence without reopening the completed migration.

## OPS-003 — Accepted image finding

The operator accepted HIGH `CVE-2026-85091` in the initial Debian trixie zlib image
for this research deployment. It was not suppressed, fixed or declared a false
positive. Recheck vendor fixes and scans on image updates; rebuild/test/release a
fixed image when available. The historical scan assessment is not a current
vulnerability verification. See [deployment evidence](DEPLOYMENT.md#last-recorded-verification).

## Completed work

DATA-002 earnings-focused selection and the bundle-exclusion portion of DATA-001
are complete. Their rules are current architecture under ADRs 011–012; remaining
production observation is OPS-002. Detailed migration/phase records remain in Git.
