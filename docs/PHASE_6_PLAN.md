# Phase 6 implementation plan — scheduling and continuous runtime

Status: IMPLEMENTED (2026-10-08).

The original plan is retained below. Delivered contracts are recorded in LLD
sections 2.6/8.2, runnable commands/settings in README, and measured replay results
in [PHASE_6_REPLAY.md](PHASE_6_REPLAY.md). The implementation uses a one-second
scheduler tick ceiling; shutdown and successful reload wake through supervised
lifecycle/events. There is no separate adaptive deadline wake mechanism. Runtime
and recovery share existing locks and the SEC budget; no fairness/cache layer was
added because the replay did not demonstrate starvation. Provider selection,
container cutover and cloud deployment remain deferred.

Prepared on 2026-10-08 against the current Phase 5 code and a clean working tree.
This plan supplements HLD, LLD, accepted ADRs and MIGRATION_PLAN; it does not
authorize live operations or subsequent migration phases.

## Outcome and scope

Deliver one supervised asynchronous application that coordinates calendar refresh,
company polling, filing enumeration, artifact acquisition/publication and repeated
recovery. Schedules and bounded queues stay in memory; existing repositories remain
the authority for durable progress. Every SEC operation uses the existing shared
client, limiter and bounded executor, with the five-request/second ceiling intact.

Add `python -m finbot_ingestion.main` as an application entry point while retaining
the legacy CLI. Production container cutover/legacy removal remain Phase 7;
CDK, GitHub Actions delivery and deployed alarms remain Phase 8. Live calendar
integration, universe seeding, terminal redrive and completed-package rechecks
remain deferred. Implementation and replay use synthetic companies and providers.

## Findings that shape the implementation

- `DiscoveryService.discover()` persists observed filings and returns accessions.
  `IngestionWorker.discover_company()` then processes each filing and its new
  artifacts inline. Runtime polling must use discovery separately so one package
  does not hold a company polling worker through all its downloads.
- `enumerate_filing()` returns known child identities; dispatch these directly,
  preserving the existing protection against delayed GSI visibility.
- `RecoveryService.run_pass()` already traverses all candidate pages and handles
  per-row failures. Share its worker/locks with normal runtime work. Repeat passes
  because an empty eventual-index query is not proof of completion.
- Calendar upserts replace entire event rows. Store satisfaction separately so
  provider refreshes, cancellation tombstones and date moves cannot erase it.
- `CalendarSyncService` has one refresh lock, separate full/near-term successes,
  scope and freshness. Share one instance and use those facts when reporting health.
- `BlockingExecution` deliberately awaits blocking I/O even after cancellation.
  A shutdown grace deadline cannot be presented as a hard bound on thread completion.
- Existing discovery conditionally creates every accession on every response.
  Measure DynamoDB calls as well as SEC throughput in replay; do not infer database
  capacity or polling latency from the SEC request ceiling.
- Filing currently carries no SEC item metadata, and the submissions parser does
  not preserve it. Add typed source evidence to discovery before implementing
  earnings satisfaction; form type alone is insufficient for an 8-K match.

## Planned defaults and policy decisions (implemented)

All durations and bounds below are initial configurable choices, not architectural
requirements. Validate finite positive values, relationships and count bounds.

| Setting | Initial proposal |
| --- | --- |
| Market timezone / reference calendar | `America/New_York` / `XNYS` |
| Active polling | 10 seconds |
| Safety polling | 1 hour, deterministically staggered by CIK |
| Before-market window | Session open minus 2 hours through open plus 2 hours |
| After-market window | Session close minus 2 hours through close plus 3 hours |
| Unknown-time window | Session open minus 2 hours through close plus 3 hours |
| Grace after an unsatisfied window | 2 hours at active cadence, then safety polling |
| Non-session expectation | Dated 07:30–19:00 market-local fallback, then grace |
| Universe/calendar reload | 5 minutes and after successful refresh |
| Recovery pass | 60 seconds after the previous pass completes |
| Scheduler wake ceiling | 1 second; wake earlier for schedule changes/shutdown |
| Company polling concurrency | 2 asynchronous workers |
| Filing enumeration concurrency | 1 asynchronous worker |
| Artifact concurrency | Existing WorkflowConfig limit, default 2 |
| Queued company / filing / artifact bounds | 1000 / 100 / 200 |
| Metrics flush / health heartbeat | 5 seconds / 30 seconds |
| Shutdown drain grace | 30 seconds before cooperative cancellation |

Use the actual exchange session open/close for holidays and early closes; DST
conversion belongs in one window policy. Interpret normalized earnings dates as
market-local dates. A non-session expectation remains on its supplied date; never
silently move it to the next trading day. Safety polling runs on all calendar days.
Use half-open window intervals and UTC instants internally. Derive the reload
lookback from configured windows/grace so events crossing midnight remain eligible.

Use a small `MarketSessions` interface with a synthetic implementation for policy
tests and an `exchange_calendars` adapter for runtime. Verify and constrain its
dependency version during implementation. Missing/out-of-range session coverage
must be visible; distinguish a confirmed non-session from a calendar lookup error.
Do not fabricate holiday logic. Reviewed sources: the library's
[session API](https://github.com/gerrymanoim/exchange_calendars) and
[NYSE trading calendar](https://www.nyse.com/trade/hours-calendars).

### EarningsSatisfactionPolicy — authorized v0 rule

Implement a pure, deterministic `EarningsSatisfactionPolicy` in
`scheduler/earnings_satisfaction_policy.py`, with initial policy version
`earnings-satisfaction-v1`. Its explicit inputs are the expectation, calculated
window/grace bounds, canonical durable Filing and accession-bound SEC item metadata.
It performs no I/O, document interpretation or clock reads. Its typed decision
contains eligibility, a stable reason code and policy version.

A filing must have the same normalized CIK and a SEC acceptance timestamp
`filed_at` in the half-open interval `[window_start, grace_end)`. Only these forms
can then satisfy the expectation:

| Exact original form | Additional requirement | Persisted match reason |
| --- | --- | --- |
| `10-Q` | None; SEC item metadata is not required | `original_10_q` |
| `10-K` | None; SEC item metadata is not required | `original_10_k` |
| `8-K` | Unambiguous SEC item metadata contains exact Item `2.02` | `original_8_k_item_2_02` |

`10-Q/A`, `10-K/A` and `8-K/A` never satisfy an expectation, including an amended
8-K reporting Item 2.02. An original generic 8-K, or an 8-K with absent, malformed,
conflicting or ambiguous required item metadata, remains ineligible. Never infer
Item 2.02 from an EX-99.x exhibit, filename, document text, description or another
accession. A negative/unknown decision leaves the expectation unsatisfied and
aggressive polling eligible through the window/grace; expiry still falls back to
safety polling. All six supported forms and their artifacts continue through
normal ingestion regardless of the satisfaction decision.

Among eligible candidates, choose the earliest acceptance timestamp, then accession
as a deterministic tie-breaker. Match one expectation only; ambiguous overlapping
expectations remain unsatisfied and emit a diagnostic. Historical accessions outside
the window never satisfy a current event. Rediscovery of an already durable matching
filing with unambiguous evidence can repair a lost satisfaction checkpoint.

Persist the exact policy version, match reason and relevant normalized source
evidence with the checkpoint. Log non-match reason codes such as `amendment`,
`missing_sec_items`, `ambiguous_sec_items`, `missing_item_2_02`, `cik_mismatch` and
`outside_window`; do not create a satisfaction checkpoint for these decisions.
Future semantic changes require a new policy version and an explicit checkpoint
compatibility/re-evaluation decision. Unknown versions must not silently suppress
polling. Do not add a configurable bypass that accepts generic filings.

Stop aggressive polling only after the satisfaction write succeeds. Safety polling
and pending package/artifact recovery continue. Satisfaction does not wait for
document publication: enumeration/acquisition remain independently recoverable.
This policy is a scheduling heuristic only. Neither its decision nor its checkpoint,
logs or metrics is proof that earnings were extracted or validated.

## Implementation sequence

### 1. Runtime configuration, clock and window policy

Add `runtime/config.py`, `runtime/clock.py`, `scheduler/market_sessions.py` and
`scheduler/window_policy.py`. Compose existing SEC, DynamoDB, storage/messaging,
workflow and CalendarConfig settings without implicit `.env` loading or client
creation. Use UTC wall time for market decisions and a monotonic clock for waits,
elapsed time and deadlines. Tests inject both clocks and a cancellation-aware sleeper.

Expose active/safety intervals, window offsets, grace, coarse reload/recovery
cadences, queue capacities, worker counts and shutdown/health timing. Keep the
existing maximum-company/calendar bounds; validate requested calendar ranges
against DynamoDBConfig. On clock jumps, recompute eligibility without generating
catch-up polls for every missed interval.

Deliverable: pure window tests covering DST, holidays, early closes, unknown times,
non-session expectations, midnight boundaries, grace and overlapping windows.

### 2. Durable expected-event satisfaction

First add typed `SECItemMetadata` evidence and a discovery-batch contract associating
each observation with its accession and normalized CIK. Evidence distinguishes
known normalized items, absent metadata and ambiguous metadata. Normalize only
supported SEC source encodings into a sorted unique tuple of exact item codes;
do not use substring matching or guess unsupported encodings. Metadata for another
row/accession, misaligned item columns and conflicting duplicate observations cannot
provide a positive match. Missing/ambiguous optional item metadata must not prevent
an otherwise valid filing from being ingested.

Extend the submissions adapter/client and discovery service with an evidence-aware
path, parsing filings and item metadata from the same fetched response. Preserve
existing public entry points as wrappers returning their current contracts. Use
canonical strongly read filing identity/form/time for policy evaluation and verify
that fresh evidence belongs to that filing. Older durable filings with no item
evidence do not imply Item 2.02; a fresh valid observation can provide it. Persist
positive evidence with satisfaction without rewriting original filing provenance
or changing ArtifactReady. All source-specific parsing stays inside SEC adapters.

Add typed `EventIdentity`, `EarningsSatisfactionDecision` and `EventSatisfaction`
contracts and a focused repository protocol/adapter backed by the existing Calendar
table. Keep satisfaction separate from mutable ExpectedEarningsEvent and immutable
Filing/Artifact contracts. Require a positive versioned decision before persistence;
validate that its reason agrees with the original form and required item evidence.

Event identity is `(provider, CIK, provider_event_id)` when a stable provider ID
exists; otherwise `(provider, CIK, expected_date)`. Use a tagged, reversible encoding,
not a hash or ambiguous separator concatenation. Refresh timestamps, ticker aliases
and time-of-day changes do not create new identities. A stable-ID date move retains
satisfaction; a move without a stable ID cannot be reliably linked and may rearm
polling for the new date. Document and test that limitation.

Proposed new Calendar record shape:

```text
expected_date = "__event_satisfaction__"             # existing partition key
cik = compact JSON of the tagged EventIdentity        # existing sort key
calendar_record_type = "satisfaction"
repository_schema_version = 1
provider, company_cik, provider_event_id OR identity_date
matched_accession_number, matched_filed_at, satisfied_at
observed_expected_date, window_start, window_end, grace_end
match_policy_version = "earnings-satisfaction-v1"
match_reason = original_10_q | original_10_k | original_8_k_item_2_02
matched_form_type
matched_sec_items, sec_item_evidence_source          # required for an 8-K match
```

No table or index changes are needed. Satisfaction records are outside ISO-date
queries and have separate validation/serialization. Validate key/item limits,
retain the first compatible checkpoint and strongly read after ambiguous writes.
Conflicting identities/provenance/reasons/policy evidence fail explicitly. Validate
the canonical filing, CIK, acceptance time and policy decision before writing; no
phantom filing or unsupported reason/version is accepted. Reload validates the
checkpoint's reason/form/items/version before it can suppress polling. No TTL or
cleanup in Phase 6.

New access patterns and guards:

| Pattern | API / consistency | Frequency / bound |
| --- | --- | --- |
| Load satisfaction for current expectations | `GetItem`, strong, by complete reserved key | Startup/coarse reload; at most the bounded loaded event count; cache in memory |
| Satisfy an expectation | Conditional `PutItem` with `attribute_not_exists(expected_date)`; strong reread on conflict/lost acknowledgment | Once per logical event, plus finite checkpoint retries |
| Validate matched filing | Existing filing `GetItem`, strong | Matching candidates only, bounded by the returned submissions batch |

Keep records compact (target under 2 KiB; enforce existing 400-KB validation and
measure serialized size in tests). There is no public caller or tenant boundary;
keys derive from validated internal records. Future task-role permissions must
include this reserved partition alongside calendar sync metadata. Existing TLS,
encryption/retention policy and four-table/index contracts continue unchanged;
infrastructure configuration is still Phase 8. This is an additive schema change,
not a new stream/outbox or whole-application data-model redesign.

AWS conditional-write behavior was checked through read-only AWS MCP documentation:
[condition expressions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Expressions.ConditionExpressions.html).
Keep the existing low-level client, serializer and bounded SDK execution.

Deliverable: Stubber and stateful mocked tests for duplicate writes, conflicts,
lost acknowledgments, restart, refresh preservation, date moves and old rows.
Add table-driven pure policy/parser tests for original 10-Q/10-K, original 8-K with
Item 2.02 alone or among other items, generic 8-K, every amendment, missing/null/
empty/malformed/ambiguous items, near-match codes, column misalignment, duplicate
conflicts, wrong CIK, interval boundaries and deterministic candidate ordering.
Integration tests prove that ineligible filings are still ingested, do not create
satisfaction checkpoints and leave aggressive polling eligible; positive matches
retain the exact reason/version/evidence through refresh, restart and lost writes.

### 3. Bounded queue and in-memory polling scheduler

Add `ingestion/work_queue.py` and `scheduler/polling_scheduler.py`. Use bounded
`asyncio.Queue` instances for company polls, enumeration and artifacts. Deduplicate
by CIK/accession/artifact across queued AND in-flight work. A repeated due tick
does not create another task or postpone already queued work indefinitely.

Maintain due times in memory, with deterministic ordering/staggering. Recompute
cadence from the current window and satisfaction snapshot. After a completed poll,
schedule the next due time from completion; never enqueue a burst of missed polls.
Window transitions can bring a safety-scheduled company forward to active polling.
Removing/disabling a company invalidates queued polls before dispatch while leaving
durable pending filing/artifact work eligible for recovery.

Company queue admission is nonblocking: a full queue leaves the company due for a
later tick. Filing/artifact handoff uses bounded backpressure. Stage separation
ensures company workers never await artifact capacity; no consumer produces into
its own queue. Unqueued child work remains durable and indexed for recovery.

Use the existing shared worker/IdentityLocks registry in all paths. Add an
enumeration-only worker method that preserves parent terminal/DLQ handling and
returns known children; retain existing inline entry-point behavior for Phase 4
callers/tests. Runtime company workers use evidence-aware discovery, evaluate
EarningsSatisfactionPolicy, persist only positive decisions and enqueue unfinished
filings regardless of match outcome; enumeration workers dispatch known
children; artifact workers call `process_artifact()`.

Do not immediately increase SEC executor concurrency or change HTTP/session
semantics. Begin with the existing single blocking SEC worker. A slow request or
transport retry still delays subsequent HTTP operations; measure this in replay.

Deliverable: bounded-growth, queue-full, duplicate, cancellation, disabled-company,
window-transition and stage-backpressure tests with no deadlock.

### 4. Startup, refresh and recurring recovery

Add `runtime/application.py` to own component composition and lifecycle.

Startup loads all enabled-company pages, the near-term calendar including derived
lookback, durable satisfaction and sync state. Construct one SEC client/limiter,
one SEC executor, shared SDK executions, one CalendarSyncService, one discovery/
ingestion worker/lock registry and one RecoveryService. Validate an empty or
oversized universe explicitly rather than inventing companies.

Start a tracked initial recovery pass and initial calendar refresh before enabling
normal scheduling. Allow these to run alongside scheduling once state has loaded;
do not hold startup indefinitely waiting for every old artifact. Expose startup
recovery-in-progress until that pass completes. Repeated recovery runs never overlap
and continue all pages; call the existing `run_pass()` against the same worker.
Queue duplicates across recovery/normal dispatch are safe through identity locks
and strong checkpoint validation. Do not treat one pass as global completion.

Use CalendarConfig full/near-term cadences with one shared sync lock. Coalesce
simultaneously due refreshes; full refresh covers near-term work. Consult separate
durable success times to determine whether refresh is due after restart. Failed
refreshes retry on a bounded cooldown using existing retry classification/policy,
not on every scheduler tick. Reload universe/calendar/satisfaction coarsely and
after successful refresh; do no DynamoDB reads on scheduler ticks.

Provider outages preserve expectations and show degraded calendar freshness.
The placeholder remains explicitly unconfigured: emit a visible degraded status,
keep durable expectations and safety polling usable, and never report a successful
sync. A factory rejects unsupported configured providers. Tests inject a fake;
no live provider or production universe is selected by this phase.

### 5. Supervision, health and shutdown

Supervise every long-lived task, including workers, reload, refresh, recovery and
metrics. Unexpected exceptions or an unexpected normal return from a required
loop fail the application, cancel siblings safely and produce a nonzero exit.
Expected per-company SEC failures preserve scheduler eligibility and use bounded
backoff; shared credential/permission/missing-resource and programming failures
remain fatal. Recovery retains its existing per-row error summaries.

Expose an in-process health snapshot and atomic local JSON heartbeat file for
future container checks; add no inbound HTTP API. Separate liveness from degraded
calendar/SEC operation. Include task status/progress, heartbeat age, queue depth
and oldest age, last recovery completion/errors, last calendar full success,
scope match, freshness and provider configuration. An idle worker is healthy;
a task exiting or exceeding a documented operation/stall allowance is not.
The file is per-process operational state, outside the shared data root, and its
absence/staleness must fail a future external check. Clean it up on shutdown.

SIGTERM/SIGINT first stop producers and admissions, then drain queued/in-flight
work within the configured grace. Cancel remaining work cooperatively, await all
tasks/blocking calls, flush metrics and only then close clients/executors. Do not
release byte/I/O admission while a background call still runs. Unstarted queue
items can be discarded: filings/artifacts are durable; company polls rebuild on
startup. Record a drain timeout explicitly. Existing transport timeouts do not
provide a total streaming-download deadline; forced termination can still occur
under an external supervisor. Verify that restart repairs interrupted boundaries.

Deliverable: failures and unexpected returns in every required loop, degraded
placeholder/outage behavior, stall/heartbeat checks, SIGTERM at durable boundaries,
repeat cancellation and resource-close ordering tests.

### 6. Structured logging and metrics

Add `observability/{logging,metrics,health}.py`. Produce sanitized JSON logs and
CloudWatch Embedded Metric Format on stdout using an injectable sink and a bounded,
thread-safe accumulator. SEC instrumentation runs in executor threads; it must not
perform blocking metrics calls or access asyncio objects directly. Flush compact
EMF records every five seconds and on shutdown. Use finite metric values and
consistent units; exclude CIKs, accessions, artifact IDs and request IDs from metric
dimensions. Keep those identifiers in logs only.

Emit the LLD request/throttle/error, discovery/storage/publication/retry and latency
metrics, plus queue delay/depth, active companies, recovery errors, task health,
calendar freshness/scope validity and satisfaction observations. Count actual HTTP
attempts including retries/redirects; count discoveries/storage transitions only
when durable progress is newly observed. Repair/restart emissions are operational
observations, not an exactly-once audit ledger. Avoid double-counting by attaching
hooks at defined transport/checkpoint boundaries rather than parsing log strings.

Retain individual bounded latency samples for replay/percentile analysis; do not
substitute aggregate averages for p99. `filed_at` is SEC acceptance time, so label
discovery/ingestion latency as acceptance-based, not measured public-availability
latency. Report clock anomalies rather than silently emitting invalid durations.
Calendar freshness comes from full success AND recorded universe/date coverage;
near-term success cannot conceal stale full synchronization.

EMF is supported via log ingestion and needs no extra synchronous metrics client:
[AWS EMF documentation](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format.html).
Actual CloudWatch collection, alarm configuration and DLQ-depth monitoring remain
Phase 8. Phase 6 tests validate emitted records locally.

### 7. Capacity replay, regression and documentation

Add fake-clock scheduler/runtime tests and a deterministic roughly 500-company
replay using the actual queue, worker dispatch and shared limiter with synthetic
SEC/AWS/provider boundaries. Include 5, 10, 25 and 50 simultaneously active
companies; test both 5-second and 10-second polling configurations, large exhibit
packages, slow downloads, retries, recovery backlog and restarts.

Report per-class/per-company queue delay, effective polling intervals, request
starts/statuses, acceptance-to-discovery/storage p50/p99, maximum queue and active
task counts, persistence-call counts and completed/deferred/failed work. Assert
the global rolling SEC ceiling, bounded memory/queues and service of later work
after failures. A simulation is capacity evidence under stated assumptions, not
a live production latency guarantee. Twenty-five active companies at five-second
polling already use five submissions requests/second before indexes/downloads.

Begin with FIFO queues and bounded stage workers. Add simple measured fairness
only if replay demonstrates starvation. Likewise, add a bounded canonical-filing
cache only if repeated creation/read costs materially dominate; never let a cache
mark unfinished work complete or weaken restart checks. Record replay findings and
any resulting dispatch change in LLD before declaring acceptance complete.

Run repository-local validation after implementation:

```bash
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m pytest
git diff --check
```

Run entry-point/import checks with injected fake configuration and boundaries.
Keep unit/integration/replay network blocking. The documented 496 tests are a
historical Phase 5 result, not a fresh validation of this plan or Phase 6.

Update README and `.env.example` for runtime invocation, defaults, satisfaction
semantics, health, placeholder limitations and shutdown behavior. Document the
policy version, eligible original forms, Item 2.02 evidence, fail-closed behavior,
persisted reason and the scheduling-only meaning of satisfaction. Update LLD for
concrete interfaces/schema/algorithms, HLD if responsibilities materially change,
and MIGRATION_PLAN for actual delivered status, fresh validation and Phase 7 as
the next milestone only after all Phase 6 criteria pass. An ADR is needed only
if an accepted architectural decision changes; this plan preserves them.

## Completion checklist

- Market-aware polling, safety, grace and reload behavior are deterministic and
  tested; no durable read happens on scheduler ticks.
- Satisfaction survives restart and provider refresh and does not suppress safety
  polling or pending ingestion recovery.
- Only the same-CIK, in-window/grace original 10-Q, original 10-K or original 8-K
  with unambiguous SEC Item 2.02 can satisfy an expectation. Amendments, generic
  8-Ks and unknown/ambiguous required item metadata keep the expectation unsatisfied.
  Positive checkpoints preserve and validate match reason, evidence and policy
  version; no output claims earnings extraction or validation.
- All queues, task creation and document workflows are bounded; duplicate due
  work and backpressure cannot deadlock dispatch or grow memory indefinitely.
- Every SEC attempt retains the existing shared request ceiling.
- Recovery covers old/disabled-company work and continues empty token-bearing
  pages, failed/deferred candidates and later eventual-index entries.
- Required task failures cannot leave a healthy-looking idle process; stale or
  unconfigured calendar coverage is visible without fabricating successful syncs.
- Signal shutdown/resource cleanup and restart at interrupted durable boundaries
  are verified with offline fakes.
- Replay measurements and limitations, fresh regression results and concrete
  operational commands are documented. Legacy workflows still pass.

## Remaining inputs

The satisfaction policy above is explicitly specified by the user; it requires no
further policy confirmation. Listed timing/capacity defaults remain configurable
initial choices. A live calendar provider and authoritative production universe
are still required before production use, but do not block offline Phase 6
implementation.
