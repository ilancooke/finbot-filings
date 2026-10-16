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

## OPS-001 — Historical versus live discovery latency (closed)

Closed by ADR 021 on October 15, 2026 Pacific: retain `DiscoveryLatencyMs` and
original timestamps, but omit the latency alarm during research operation.
The specifically requested production alarm was removed without a service restart;
CDK enables future alarm creation only through explicit opt-in. Revisit alarm
applicability and thresholds when there is a customer latency commitment.

## OPS-002 — Observe live selection and daily calendar operation

Filtering subsequently stored/published DAL's `0000027904-26-000038/dal-20260930.htm`
before the explicit acquisition reset. After restart, HOVR's October 9 10-Q and
earnings 8-K produced three selected/stored/published HTML artifacts under the
new readable keys; size, acceptance provenance and parent/child checkpoints were
verified with no retries/pending/terminal facts. Continue observing selected
release/supplement artifacts, exclusions and routing warnings as polling repopulates state.
Verify ongoing daily Yahoo scope/freshness, coverage gaps, provider
access arrangements, recovery reliability and sustained memory use. Local/cloud
collection success is not complete all-company coverage or long-term reliability.
Close observations on recorded evidence without reopening the completed migration.

The October 16 UTC alarm investigation confirmed no successful Yahoo refresh since
October 13, and repeated `IncompleteCalendarSnapshot` failures from October 14,
before the universe expansion. The stale alarm crossed the 48-hour age bound; the
current scope alarm followed the saved 50-company snapshot's mismatch with 72
companies. Follow-up live inspection showed that Yahoo's pagination counts distinct tickers,
while a company may expand into multiple event rows: 192 companies produced 194
events. The adapter currently compares event-row counts against company counts.
The local pagination fix counts tickers and preserves all events, retaining
bounded collection, terminal-page evidence, verification passes and curated-company
ambiguity safeguards (ADR 022). A read-only 30-day collection check
completed for all 72 companies with the normal bounds (45 observed companies;
27 without observations in that window). Production delivery remains pending.
Verify a complete refresh and its durable 72-company scope/freshness; do not clear the alarms by overwriting health state.
See the dated investigation in DEPLOYMENT for evidence and limits.

## OPS-003 — Accepted image finding

The operator accepted HIGH `CVE-2026-85091` in the initial Debian trixie zlib image
for this research deployment. It was not suppressed, fixed or declared a false
positive. Recheck vendor fixes and scans on image updates; rebuild/test/release a
fixed image when available. The historical scan assessment is not a current
vulnerability verification. See [deployment evidence](DEPLOYMENT.md#last-recorded-verification).

## OPS-004 — Automate application and infrastructure releases

The FilingReady/topic cutover required manual delivery-flag toggles, ECS stops,
image-digest copying, separate CDK deployments and baseline-ARN updates. The split
between GitHub application delivery and manual infrastructure deployment makes
combined releases cumbersome and prone to configuration mistakes.

Consider extending the existing GitHub Actions/CDK setup into one release workflow:
build/test an immutable image once, pass its digest into CDK, present the deployment
diff for production approval, deploy stacks in order, update the baseline, and
activate/verify the service. Automate the handoffs without routine flag toggles
or copying digests/ARNs. Target operator flow: push to main, successful CI, approve.

Revisit ADR 007's ownership split and provide a scoped OIDC infrastructure role;
the current release role cannot deploy CDK. Preserve serialized releases, actual
STOPPED handoffs, stopped environments unless activation is requested, compatible
rollback/recovery and release evidence. Use DEPLOYMENT.md's current procedure as
the starting contract.

## Completed work

DATA-002 earnings-focused selection and the bundle-exclusion portion of DATA-001
are complete. Their rules are current architecture under ADRs 011–012; remaining
production observation is OPS-002. Detailed migration/phase records remain in Git.
