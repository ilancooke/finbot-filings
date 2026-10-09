# SEC ingestion migration plan

This is the execution roadmap for repurposing `repos/finbot-filings`. Read it at
the start of a migration session, inspect the current working tree, and implement
only the phase or scope authorized by the user. Phase completion is not automatic
authorization to begin the next phase.

## 1. Target state

This repository is becoming a focused SEC document-ingestion service. It will
discover relevant filings for a curated universe of approximately 500 companies,
acquire primary documents and all attached documents/exhibits, preserve immutable
raw artifacts in S3, maintain metadata and durable checkpoints in DynamoDB, and
publish versioned `ArtifactReady` events through SNS after durable storage.

The architectural sources of truth are:

- [README.md](../README.md): project scope and current implementation status.
- [docs/HLD.md](HLD.md): architecture, responsibilities, constraints, and boundaries.
- [docs/LLD.md](LLD.md): contracts, persistence, algorithms, configuration, and tests.
- [docs/adr/](adr/): accepted architecture decisions and their rationale.

This plan sequences implementation; it does not replace those design documents.
Legacy code/docs were implementation/reference material, not the specification
for the new service. Phase 7 removed them; the complete pre-cutover revision is
`59cacd78a1da01d00b913c2e67185b0a0980d7ce` (recovery command in README).

The target Python namespace is `finbot_ingestion`. The physical repository and
distribution remain `finbot-filings` during migration; a repository rename is not
a prerequisite. Legacy `finbot_filings` coexisted through Phase 6 and was removed
during Phase 7. Only `finbot_ingestion` is now installed.

The approved v0 architecture includes:

- Calendar-driven per-company polling, with low-frequency safety polling outside
  active earnings windows. Calendar expectations are hints; SEC is authoritative.
- One continuously running ECS/Fargate task, an in-memory scheduler/request queue,
  and one centralized SEC request budget capped at five outbound requests/second.
- Relevant forms: `8-K`, `10-Q`, `10-K`, `8-K/A`, `10-Q/A`, and `10-K/A`.
- Filing identity by accession; child identity by accession plus original filename.
- S3 raw storage, DynamoDB metadata/state, SNS fanout, and consumer-owned SQS queues.
- CloudWatch logs/metrics/alarms and an operational dead-letter path.
- Application and CDK code in this repository; eventual GitHub Actions application
  delivery, with manual infrastructure deployment initially.

Company Facts, global EDGAR feed ingestion, distributed rate limiting, multiple
ingestion tasks, RAG, earnings extraction, financial interpretation, and feature
generation are outside v0. Content hashing is intentionally excluded. Legacy
parser hash requirements do not justify adding hashing to the ingestion contract.

## 2. Migration principles

1. Incrementally reuse/refactor existing SEC acquisition code rather than rewrite
   working parsing, URL construction, identification, and byte-preservation logic.
2. Keep legacy functionality working until its replacement is validated. Preserve
   outstanding user changes before removing any legacy code; do not assume an
   uncommitted working tree is recoverable from Git history.
3. Preserve immutable SEC source artifacts and point-in-time history. Amendments
   and restatements are new accessions, never replacements for prior filings.
4. Keep downstream interpretation/extraction outside this service. Raw XBRL files
   may be acquired as artifacts, but fact, section, and taxonomy interpretation
   must not become prerequisites for acquisition.
5. Persist durable facts/checkpoints, not high-frequency transient states such as
   `downloading`. Short-lived scheduling and queue state remain in memory.
6. Route every SEC request, including index requests, document downloads, retries,
   and followed redirects, through the same outbound budget. Scheduler eligibility
   does not guarantee an immediate request.
7. Keep provider/SEC response structures and AWS SDK objects inside adapters;
   expose typed contracts and independently testable interfaces.
8. Preserve existing shared data. Do not migrate, delete, or commit operational
   data as a side effect of changing source code. This service's approved cloud
   contract does not require a parallel local Parquet/catalog pipeline.
9. Keep changes scoped to this repository. Downstream queues and downstream
   interpretation packages remain owned by their consumers.
10. Do not introduce architectural changes or unapproved services silently. Update
    the appropriate HLD/LLD/ADR when changing architecture. Implementation details
    delegated to configuration do not require reopening accepted decisions.
11. Use offline fixtures, fake clocks, and mocked boundaries for routine tests;
    live SEC smoke tests are optional and manual only.
12. Prefer a clean final package over a permanent legacy subtree. Preserve useful
    legacy work in a recoverable revision, then remove superseded code after
    cutover. Moving it into a consumer repository is separate, authorized work.

## 3. Full transition plan

Sequence: contracts/parsing → SEC transport/package discovery → DynamoDB →
restart-safe S3/SNS acquisition → calendar → scheduling/runtime → package cutover
→ CDK/application delivery. Each phase must leave an independently testable state.

### Phase 1 — Establish target contracts and reuse submissions parsing

**Status: COMPLETE.**

**Goal:** Establish executable target contracts and an offline submissions parser
by adapting the existing SEC foundation, while leaving the legacy CLI functional.

**Major modules/files:**

- `src/finbot_ingestion/domain/{company,calendar,filing,artifact,events,identity,validation}.py`
- `src/finbot_ingestion/config.py`
- `src/finbot_ingestion/sec/{submissions,urls}.py`
- `tests/unit/`, `tests/fixtures/submissions_mixed.json`
- `pyproject.toml`, `.env.example`, `README.md`, `docs/LLD.md`

**Existing code reused:** Dataclass and validation patterns from legacy
`models.py`; accession validation, URL construction, parallel-array parsing, and
deduplication principles from `sec/filings.py`; configuration validation patterns
and offline test approaches. The new package must not import legacy parsing,
XBRL, HTTP, or persistence modules.

**New functionality:** Normalized CIK-based companies, all six forms,
timezone-aware publication/discovery fields, deterministic child identities,
calendar expectations, durable artifact fields, and versioned event serialization.
Validate identifying User-Agent and a positive finite request ceiling at most five.
This phase validates the ceiling; actual enforcement belongs to Phase 2.

**Testing expectations:** Pure offline fixtures for forms, identities, UTC
timestamps, malformed arrays/rows, duplicate conflicts, optional primary names,
configuration, and event serialization. Keep the existing 144-test baseline green.

**Acceptance criteria — all satisfied:**

- [x] `finbot_ingestion` imports alongside the existing package.
- [x] Typed `Company`, `ExpectedEarningsEvent`, `Filing`, `Artifact`, and
  `ArtifactReady` models implement the LLD fields, without transient states.
- [x] CIKs normalize to ten-digit strings; accessions retain dashed identity;
  SEC archive URLs use numeric CIK and dash-free accession path components.
- [x] Artifact identity unambiguously encodes accession and original filename,
  independently of ticker/content. Traversal and invalid filenames are rejected.
- [x] A mixed submissions fixture returns all six forms, preserves amendments
  as separate accessions, and deduplicates repeated accessions without a count cutoff.
- [x] Valid source timestamps normalize to aware UTC datetimes; missing, malformed,
  or naive intraday timestamps fail explicitly rather than becoming midnight dates.
- [x] Optional report dates and missing primary-document names do not block
  discovery when subsequent package enumeration can supply the document identity.
- [x] `ArtifactReady` serializes `artifact.ready`/`1.0`, contains no document
  contents, and requires durable artifact-location/timestamp fields.
- [x] Configuration requires SEC identification, rejects request ceilings above
  five, and does not require legacy output directories.
- [x] New tests use local fixtures/fakes, make no SEC/AWS calls, and do not touch
  shared operational data.
- [x] All 144 legacy tests plus new tests pass; compilation checks pass.
- [x] Documentation explains contracts, coexistence, and remaining phases; no
  legacy code or outstanding user work was deleted by Phase 1.

**Concrete delivered choices:** `Artifact.artifact_id` is computed as
`<dashed-accession>/<original-filename>`. `filed_at` uses SEC `acceptanceDateTime`,
not an independently measured public availability time. Identical accession rows
collapse; conflicting rows reject the parse. Missing primary names become `None`.
`s3_uri`/`stored_at` are paired checkpoints; publication requires storage.
`IngestionConfig.from_env()` reads environment or an injected mapping, not `.env`
implicitly. These details are recorded in LLD section 2.1.

### Phase 2 — Implement shared SEC transport and complete package discovery

**Status: COMPLETE.**

**Goal:** Provide fresh SEC responses, a single reliable outbound request budget,
and complete filing-package enumeration independent of XBRL availability.

**Major modules/files:**

- `src/finbot_ingestion/sec/{client,rate_limiter,filing_index,errors}.py`
- Existing `sec/submissions.py`, `sec/urls.py`, and `config.py` as needed
- `src/finbot_ingestion/ingestion/retry_policy.py` for centralized retry policy
- Unit tests and SEC package replay fixtures; README/LLD interface notes

**Existing code expected to be reused:** Legacy `SECClient` identification,
connection reuse, timeout/error and exact-byte behavior; Phase 1 domain/parser/URL
contracts; generic directory parsing and accession URL logic from
`xbrl/download.py`; filename-safety and byte-preservation tests. Do not carry over
the permanent JSON cache, independent ten-request/second limiters, or the
requirement for exactly one specialized XBRL ZIP/instance pair.

**New functionality:** One shared limiter for all SEC endpoints and attempts;
fresh submissions and incomplete-package responses; typed response/download
metadata; retry/error classification with configurable backoff/jitter and
per-attempt logs; primary-document plus all attached-document enumeration retaining
original names and document types where available. Package discovery must handle
temporarily incomplete metadata without declaring it complete prematurely.

**Transport design note:** Centralized request budgeting and reliable package
discovery are requirements. Asynchronous HTTP transport itself is not an
architectural requirement. Choose synchronous or asynchronous HTTP according to
the simplest justified design that satisfies concurrency and rate limiting. If
using synchronous I/O in the later async runtime, explain how it avoids blocking
the scheduler. Record the choice and any interface adjustment in the LLD; do not
treat the LLD's illustrative async signatures as a mandate for a particular HTTP
library. Preserve single-service ownership of the request budget either way.

**Testing expectations:** Fake sessions/transports and clocks must cover parallel
callers, all request classes, retries, HTTP/network failures, response freshness,
and exact binary bytes. Package fixtures cover ordinary non-XBRL filings, multiple
exhibits including PDFs, duplicate links, missing primary names, unsafe filenames,
and delayed package availability. Normal tests must make no live SEC calls.

**Acceptance criteria:**

- At most five outbound request starts in any rolling one-second interval across
  submissions, index, document, retry, and followed-redirect requests, including
  concurrent callers. Configured lower ceilings also take effect.
- Every SEC call uses the shared budget, identifying headers, and timeout policy;
  errors and retries remain observable and bounded.
- Repeated polls see updated submissions/package responses; indefinite URL-based
  JSON caching cannot hide new filings or delayed documents.
- Enumeration preserves the primary document and all attached documents/exhibits,
  with deterministic child identities and no duplicate logical artifacts.
- No EX-99.1-only filtering, XBRL prerequisite, interpretation, or content hashing.
- Offline unit/replay tests and existing regression tests pass. No AWS integration,
  calendar, scheduler, or continuous service runtime is introduced in this phase.

### Phase 3 — Implement durable DynamoDB repositories and access patterns

**Status: COMPLETE.**

**Goal:** Persist identity, metadata, and durable progress with atomic idempotency
and deliberate bounded recovery access patterns.

**Major modules/files:** Repository protocols in
`src/finbot_ingestion/repositories/` for companies, calendar, filings, and artifacts;
`repositories/dynamodb/{companies,calendar,filings,artifacts,serialization}.py`;
domain checkpoint additions; mocked integration tests; LLD persistence sections.

**Existing code expected to be reused:** Phase 1 identities/models/serialization
and tests. Carry forward local acquisition's skip/repair principles, but do not
reuse filesystem existence checks as the cloud source of truth.

**New functionality:** Separate DynamoDB tables as allowed by the LLD;
conditional filing/artifact creation; idempotent stored/published checkpoints;
company/calendar persistence; durable package-enumeration completion; repository
methods and sparse pending-work indexes for recovery. Add indexes only for
concrete access patterns and update them at durable boundaries.

**Testing expectations:** Mock AWS boundaries to exercise duplicate/competing
creation, conditional failures, serialization, pagination, immutable provenance,
partial enumeration, and recovery of pending work older than a convenient recent
window. Test that discovery of an existing accession does not suppress unfinished
enumeration.

**Acceptance criteria — all satisfied:**

- [x] Conditional writes produce one logical filing/artifact per identity without
  replacing original publication/discovery facts.
- [x] A crash after filing creation or partial child creation leaves recoverable work.
  Enumeration completes only after all discovered child records are durable.
- [x] Recovery uses explicit paginated/indexed access rather than unbounded scans;
  old incomplete work remains discoverable instead of silently aging out.
- [x] Durable checkpoints drive restart inference; no `downloading` or per-queue-move
  state is persisted.
- [x] Repository failure and regression tests pass; schema/access-pattern additions
  are documented before dependent services rely on them.

**Concrete delivered choices:** Four separate tables and fixed KEYS_ONLY indexes
for enabled companies, pending enumeration, pending artifact acquisition/publication,
and accession-child listing. Strong base reads validate eventual GSI candidates.
Recovery pages have scoped SDK continuation tokens, with no age cutoff or TTL.
Conditional creates and bounded revision-guarded updates preserve first provenance.
FilingCheckpoint keeps enumeration/failure facts separate from immutable Filing;
PackageCheckpoint confirms every child through primary-key reads before completion.
Completed snapshots retain their first timestamp/count/primary; later observed
snapshots can add children without overwriting those facts. No package-wide
transaction, BatchWriteItem, scans, document bytes, or hashes are introduced.

Async adapters share an injected low-level Boto3 client and bounded executor.
Separate DynamoDBConfig leaves SEC/legacy configuration requirements unchanged.
Schema version 1 uses fixed UTC timestamps, strict numeric/identity checks,
omitted optionals, JSON-encoded raw calendar payloads and size/key guards. Calendar
upserts protect newer synced_at observations; date-range queries paginate explicit
UTC dates. Company configuration remains mutable. Boto3 is the new runtime dependency.

**Limitations:** GSI visibility is eventual, so future recovery must repeat whole
passes. Enumeration completion only describes an observed snapshot. Retry counts
are chronological diagnostics, not an exact concurrent audit ledger. Low-level
mark_enumerated is used through PackageCheckpoint; database checkpoint methods do
not verify external S3/SNS commits. Reconciliation cadence, terminal/DLQ handling,
calendar date-move/cancellation logic and runtime orchestration remain later phases.
Low-cardinality work-class partitions are a deliberate v0 tradeoff, without a
production throughput or pricing claim. Future CDK must provision the documented
tables/indexes; this phase does not create or inspect AWS resources.

**Validation (2026-10-08):** 340 tests pass (270 prior tests plus 70 new Phase 3
tests); compilation and diff checks pass. SDK Stubber validates wire requests;
stateful mocked SDK boundaries exercise conditional races, lost acknowledgments,
partial child creation/parent commit, old work, pagination and stale/delayed GSIs.
Integration tests block network and use dummy credentials. Executor tests verify
event-loop responsiveness, bounded concurrency and cancellation cleanup. No live
AWS/SEC validation, resource changes, shared-data mutations or legacy removal.
README/LLD/example environment record the implementation and AWS guidance choices.

### Phase 4 — Deliver restart-safe acquisition and publication

**Status: COMPLETE.**

**Goal:** Complete the acquisition-to-event path with immutable storage,
idempotency, and recovery across every durable boundary.

**Major modules/files:** `storage/{artifact_store,s3_artifact_store}.py`;
`messaging/{publisher,sns_publisher,dead_letter}.py`;
`ingestion/{discovery_service,artifact_downloader,ingestion_worker,recovery_service}.py`;
repository recovery APIs; integration/replay tests and LLD checkpoint documentation.

**Existing code expected to be reused:** Exact-byte preservation, skip-complete,
and partial-repair behavior/tests from legacy acquisition/storage; Phase 2 SEC
client/enumeration; Phase 3 repositories. Do not reuse raw overwrite options or
the XBRL-first bundle orchestration unchanged.

**New functionality:** Conditional S3 creation under
`<cik>/<accession>/<filename>`; existing-object inspection; ordered S3 write,
DynamoDB metadata commit, SNS publish, and publication checkpoint; repair of
interrupted work; bounded retries and operational dead-letter handling.

**Testing expectations:** Offline integration replay must cover:

```text
submissions → filing → package enumeration → artifact records
→ SEC bytes → S3 → DynamoDB stored checkpoint → SNS → published checkpoint
```

Inject failures between every durable step, including a successful external call
whose acknowledgment/checkpoint is lost. Cover partial enumeration, S3/DynamoDB/
SNS failures, duplicate discovery, amendments, terminal work, and DLQ-send failure.

**Acceptance criteria:**

- [x] Raw objects are created conditionally (for example, `If-None-Match: *`) and
  never replaced on retry. Deterministic keys alone do not enforce immutability.
- [x] If S3 succeeds before the metadata checkpoint, recovery inspects the existing
  object and repairs metadata without replacing bytes or inventing new provenance.
- [x] Stored artifacts are not downloaded again merely because SNS publication fails.
- [x] If SNS succeeds before `published_at` is saved, recovery may republish the same
  logical event. Document at-least-once delivery and consumer deduplication by
  `artifact_id`; do not claim exactly-once delivery.
- [x] Restart finishes incomplete filing enumeration and artifact acquisition.
  Amendments retain separate immutable records and objects.
- [x] Events contain references/metadata only and are published only after S3 and
  DynamoDB success. Completed records require no recovery action.
- [x] Terminal failures before artifact creation are also representable; failed
  dead-letter sends do not silently discard work.
- [x] Failure/restart integration tests and legacy regressions pass.

**Concrete delivered choices:** Low-level S3 conditional PUT and HEAD inspection
with versioned ASCII canonical provenance; HEAD LastModified supplies the original
stored_at for first commit and repair. SNS receives unchanged ArtifactReady 1.0
JSON after the strong database storage checkpoint; ambiguous acknowledgments may
duplicate logical events. Operational SQS receives stable metadata-only terminal
envelopes; database terminal facts precede the send, and the first successful send
checkpoint removes pending membership. Four tables and existing GSI definitions
remain unchanged; DEAD_LETTER is an additional pending-work partition value.

ProcessingCheckpoint/ArtifactCheckpoint add separate persisted workflow stage
budgets, terminal observations and dead-letter checkpoints without changing source
Artifact or event fields. FilingCheckpoint covers terminal enumeration before
child creation. CAS guards stop late failures from terminalizing advanced work.
Partially created children defer until successful enumeration; a terminal parent's
envelope covers its blocked package. Submissions failures surface without success
watermarks and are retried on a later poll; shared credential/permission/resource
errors propagate. No invented pre-filing identity or extra failure table is used.

Discovery, acquisition/publication and explicit RecoveryService.run_pass reuse
Phase 2 transport and Phase 3 repositories/PackageCheckpoint. Known new children
are dispatched by identity/strong reads, not immediate GSI visibility. Recovery
continues all candidate pages, including empty token-bearing pages and failed or
deferred oldest rows. Shared bounded executors preserve event-loop responsiveness
and retain admission through repeated cancellation; SDK and workflow attempt logs
record failures/recovery. Per-identity locks and two whole artifact slots bound
local overlap and retained bytes.

**Implementation limits:** Default/maximum artifact size is 64 MiB, configurable
downward, enforced by streaming decoded SEC bytes. Larger artifacts fail visibly
without an unsafe multipart fallback. There is no application content hash, so
compatibility checks rely on canonical metadata and inspected object attributes.
At-least-once SNS/SQS delivery requires stable-ID deduplication. No automated
terminal redrive command, completed-package reconciliation cadence, continuous
recovery loop, calendar provider, main runtime or infrastructure is introduced.
An operator redrive contract remains future work; normal workers do not reset
terminal facts. A terminal package's partial children can remain deferred index
candidates. GSI visibility remains eventual and a single pass cannot prove global
completion. Cloud permissions, encryption/retention and conditional-write policies
must be supplied by future infrastructure. Boto3 minimum raised to 1.43.110, whose
conditional-PUT model was verified locally; no new dependency is added.

**Validation (2026-10-08):** See historical Phase 4 validation below. Tests use SDK
Stubber/stateful mocked boundaries and block network access. They replay the
complete submissions/parser/package/SEC-byte/S3/database/SNS path, all durable
acknowledgment/checkpoint losses, separate restart-persistent budgets, terminal
enumeration/acquisition/publication, failed/lost DLQ sends, pagination/old/disabled
company work, amendments, canonical ticker aliases, Unicode filenames, limits,
permission failures and cancellation. No live AWS/SEC calls, AWS changes,
shared-data writes or legacy removal. README/LLD/example environment document
the delivered contracts; accepted ADR architecture remains unchanged.

### Phase 5 — Add earnings-calendar synchronization

**Status: COMPLETE for the authorized placeholder-provider scope.**

**Authorized deviation:** The user requested implementation before selecting a
calendar provider, explicitly asking for a placeholder. The provider-independent
service/reconciliation/checkpoints and placeholder adapter are delivered; the
selected live free-provider adapter is deferred. This is not a claim of production
calendar access or verified provider completeness/coverage. No company universe
was invented. Phase 6 may use fake/placeholder boundaries during development.

**Goal:** Maintain durable provider-independent earnings expectations for the
configured universe, with safe refresh and reconciliation.

**Major modules/files:** `calendar/provider.py`, `calendar/service.py`,
`calendar/providers/<selected_provider>.py`; calendar repository reconciliation;
calendar configuration; provider fixtures and synchronization tests.

**Existing code expected to be reused:** Phase 1 `Company` and
`ExpectedEarningsEvent`, configuration patterns, and Phase 3 repositories.
There is no legacy calendar implementation to migrate.

**New functionality:** Swappable provider interface and one selected free provider;
field/time normalization; curated-universe filtering; daily synchronization and
configurable near-term refresh; moved/cancelled-event reconciliation; logging of
material expectation changes; durable successful-sync tracking.

**Testing expectations:** Fake-provider tests cover before-market, after-market,
unknown times, duplicate events, moved dates, cancellations, partial provider
failures, and restart. Credentials/provider secrets must not appear in fixtures,
logs, or committed configuration.

**Acceptance criteria:**

- [x] Provider-specific fields remain within the adapter; other components receive
  normalized expectations and optional raw diagnostic payloads.
- [x] A date move does not leave an obsolete active row under a date-based key.
  Reconcile only the range for which provider synchronization succeeded.
- [x] Failed/incomplete fetches cannot erase valid durable expectations; existing
  records remain usable through provider outages.
- [x] Record the last successful sync durably and make stale synchronization observable.
- [x] Calendar synchronization tests and regressions pass. Calendar data remains
  mutable planning data, separate from immutable filing history.

**Delivered:** CalendarSnapshot explicitly identifies exact provider/date/company
coverage and completeness; validated normalized expectations remain independent of
provider payloads. PlaceholderCalendarProvider raises CalendarProviderNotConfigured
instead of inventing empty data. CalendarSyncService exposes explicit sync_once,
full/near-term refresh and health calls; one shared instance serializes refreshes.
Enabled-company pagination, count bounds, canonical ticker/UTC observation handling,
duplicate/scope/JSON validation, finite provider/checkpoint retries and material
change logs are covered by offline tests.

DynamoDB retains four tables and existing indexes. Calendar inactive tombstones
protect cancellations against stale upserts; strong date queries omit them while
continuing pagination. Reserved calendar metadata stores revision-guarded runs,
monotonic observation timestamps, separate full/near-term success scopes and
sanitized failures. Request IDs/timestamps preserve logical retries after lost
acknowledgments. Writes precede scoped cancellation, and success follows every
durable write. Restart re-fetches a complete snapshot to repair partial progress.

**Limits/deferred work:** No live provider adapter/HTTP access, production universe,
automatic scheduler, cloud provisioning, calendar CLI, tombstone cleanup or durable
snapshot replay during provider outages. Applying a snapshot is not atomic; readers
can see partial progress. Moves beyond confirmed coverage can temporarily retain
old expectations. Provider switching is not silently performed at occupied keys.
Old calendar readers must not run with the Phase 5 writer. Health measures last
full completion age; callers inspect its scope after universe/range changes.
Provider-specific credentials, timeouts, limits and completeness evidence will be
implemented after selection. Recurring cadence belongs to Phase 6.

**Validation (2026-10-08):** 496 tests pass (414 prior tests plus 82 Phase 5 tests).
Repository-local compilation (`.venv/bin/python -m compileall -q src tests`) and
`git diff --check` pass. Real SDK Stubber requests and stateful mocked DynamoDB
boundaries cover conditional writes, pagination, date moves/cancellations,
incomplete/invalid/empty snapshots, disabled/outside-range rows, bounds, separate
freshness scopes, failed/lost checkpoints, stale retries, CAS races, cancellation,
restart at each durable boundary and secret-safe logs. All new boundaries are
offline/network-blocked. No live AWS/SEC/provider operations, resource changes,
shared-data writes, container jobs or legacy removal occurred.

### Phase 6 — Add scheduling and the continuous runtime

**Status: COMPLETE (2026-10-08).**

**Goal:** Coordinate calendar-driven polling, safety polling, acquisition, and
recovery in one supervised long-running process.

**Major modules/files:** `scheduler/{window_policy,polling_scheduler,earnings_satisfaction_policy}.py`;
`ingestion/work_queue.py`; `main.py`; `observability/{logging,metrics,health}.py`;
runtime configuration; fake-clock scheduler and load replay tests.

**Existing code expected to be reused:** Completed SEC, persistence, acquisition,
recovery, and calendar services. Reuse the centralized retry policy and typed
contracts rather than implementing independent limits in scheduler/workers.

**New functionality:** Centralized market-time/window policy; configurable active,
safety, and grace schedules; in-memory `asyncio.Queue` and duplicate-work
suppression; worker supervision; coarse calendar reloads; durable expected-event
satisfaction with matched accession, match reason and policy version; typed SEC
item evidence from submissions/discovery; startup reconstruction; SIGTERM handling;
structured logs, latency/error/health metrics, and calendar freshness tracking.

**Testing expectations:** Fake-clock tests cover active/safety boundaries,
daylight-saving changes, nontrading days, unknown report times, satisfied events,
grace periods, and restart. A roughly 500-company replay measures queue delay and
discovery/download contention. Test task failure, health reporting, and shutdown.

**Authorized satisfaction policy:** Implement deterministic versioned
`EarningsSatisfactionPolicy`, initially `earnings-satisfaction-v1`. Require the
same CIK and acceptance time within window/grace, plus an original 10-Q, original
10-K, or original 8-K with unambiguous SEC Item 2.02 metadata. Amendments and generic
8-Ks are ingested but never satisfy an expectation. Missing or ambiguous required
metadata leaves it unsatisfied and aggressive polling continues through window/grace.
Persist match reason and policy version. Satisfaction is a scheduling heuristic
only, never evidence of extracted or validated earnings. See
[PHASE_6_PLAN.md](PHASE_6_PLAN.md) and LLD section 8.2 for the implemented contract.

**Acceptance criteria:**

- Schedules/queues remain in memory; calendar/recovery state reloads from durable
  records. No DynamoDB read occurs on each scheduler tick.
- Every outbound SEC request still obeys the shared budget; duplicate due work
  does not grow the queue without bound.
- Aggressive polling stops for a satisfied expected event and remains stopped
  after restart; safety polling continues according to policy.
- Satisfaction tests cover eligible original forms, Item 2.02 among multiple items,
  generic 8-Ks, all amendments, missing/ambiguous/malformed item metadata, wrong CIK,
  window/grace boundaries and deterministic ordering. Non-matches remain ingested
  without suppressing aggressive polling; positive checkpoints retain the reason,
  evidence and policy version through refresh/restart.
- Failed background tasks cannot leave a deceptively healthy idle process.
- SIGTERM stops new work and permits reasonable in-flight cleanup; interrupted
  work remains recoverable.
- Replay reports capacity/latency behavior. Twenty-five active companies polling
  every five seconds already consume the entire five-request/second budget before
  downloads or retries; do not promise unconditional downstream latency targets.
- Add elaborate fairness only if replay demonstrates starvation; retain a simple
  design otherwise. Runtime, scheduler, and recovery tests pass.

**Delivered:** `main.py`/RuntimeApplication, finite bounded worker pools/queues,
completion-based market-aware polling, coarse durable reloads, full/near-term
refresh coordination, repeated shared-lock recovery, conditional versioned
satisfaction and typed SEC item evidence, signal cleanup, sanitized JSON logs,
bounded thread-safe EMF and local heartbeat/health checks. Actual XNYS sessions
use the constrained `exchange_calendars` dependency. Existing services and legacy
CLI remain available. No table/index change, live provider selection, infrastructure
change, shared-data write or legacy removal occurred.

**Validation:** 583 offline tests pass using the repository-local environment,
including 87 Phase 6 tests over the 496-test Phase 5 baseline. Compilation and
`git diff --check` pass. Real SDK Stubber/stateful fakes validate checkpoints,
eligibility/non-match ingestion, restart/date moves/lost acknowledgments, every
required-loop failure/return, signal admissions, stalls/freshness and repeated
cancellation with blocking cleanup. Eight 500-company virtual-clock scenarios
report SEC request starts/statuses, per-company polling intervals, stage latency,
queue/task bounds and persistence calls. See [PHASE_6_REPLAY.md](PHASE_6_REPLAY.md).
All active companies receive service and old disabled-company work hidden until a
later GSI pass is published; no elaborate fairness/cache layer was added.

**Limits:** Synthetic replay is not a production latency or database sizing
estimate. Large active sets/downloads exhaust the shared SEC budget. Only an
unconfigured placeholder is wired; a live adapter and production universe remain
external inputs. The shutdown grace is a drain allowance, not a hard blocking-I/O
deadline. CloudWatch collection/alarms and container/runtime cutover remain later
phases. Terminal redrive/completed-package rechecking remain deferred.

### Phase 7 — Complete runtime cutover and clean the package

**Status: COMPLETE (2026-10-08).**

The original plan, delivered scope, preservation gate and package/container
validation are recorded in [PHASE_7_PLAN.md](PHASE_7_PLAN.md).

**Goal:** Make the ingestion service the supported runtime and remove superseded
legacy functionality without losing valuable work or operational history.

**Major modules/files:** `pyproject.toml`, README, `.env.example`, scripts,
`Dockerfile`, `.dockerignore`, ignore rules; superseded legacy source/tests/docs;
container startup/shutdown tests.

**Existing code expected to be reused:** Migrated acquisition logic/tests and
useful fixtures. Preserve parser/extractor implementations and tests in a
recoverable revision before removal, including any outstanding user edits.

**New functionality:** Production container entry point and focused operational
commands; completed package boundary and dependency cleanup. Remove section/fact/
taxonomy interpretation, local bundle layout/storage, obsolete batch/extraction
commands, and unused dependencies after replacement validation. Do not keep a
permanent legacy subtree merely because the code once worked.

**Testing expectations:** Run the remaining suite, package/import checks, and
container startup/shutdown against fake boundaries. Rebuild the container when
code, dependencies, Dockerfile, or compose configuration changes before jobs run.

**Acceptance criteria:**

- [x] The supported entry point is `python -m finbot_ingestion.main`.
- [x] Container lifecycle tests pass; image configuration and documented commands agree.
- [x] No ingestion dependency remains on PyArrow, legacy interpretation modules,
  local dataset roots, content hashes, or raw overwrite flags. Retain only
  dependencies justified by acquisition (for example an index HTML parser).
- [x] Existing shared data remains untouched; useful legacy code is recoverable.
- [x] Downstream code relocation is not silently undertaken in other repositories.
- [x] Obsolete docs/commands are removed or clearly archived, and the final package
  is focused on ingestion.

**Delivered:** Ingestion-only package discovery and module commands; removed
`finbot_filings`, its console entry point, 144 legacy tests, four interpretation
fixtures, local batch/ticker workflow and legacy docs. Every removed tracked file
was verified unchanged against the recovery revision before deletion; no user
source edits required a separate recovery copy. PyArrow/lxml direct requirements
are removed. Existing acquisition regressions already covered original bytes,
idempotency, partial enumeration, storage repair, publication-only retry and
amendments; all 439 ingestion tests are retained.

Python 3.12 slim wheel-based Dockerfile runs as UID/GID 10001 with direct PID 1
module startup and local heartbeat health checks. Build-context allowlist/ignore
rules exclude secrets, caches, shared data and tests. README/.env.example document
the supported local/container workflows and recovery revision. Root fixtures block
network/use dummy credentials; SDK stateful fakes are shared with a mounted-only
lifecycle harness. Nine guarded subprocess checks and six opt-in Docker cases
validate package/health, actual signals, original bytes/publication, failure exits,
blocked I/O and cleanup. No application behavior/schema changes were needed.

**Validation (2026-10-08):** Before cleanup, 583 tests passed in the repo-local
Python 3.14.8 environment. After removal/additions, 448 offline tests pass with
six Docker cases explicitly skipped in the default suite; all six opt-in cases
pass against the rebuilt Python 3.12 Linux image. Compilation and diff checks
pass. Wheel/sdist contents and metadata, fresh temporary wheel installation,
all-module imports, help/health and `pip check` pass. Image installation validates
absence of the legacy namespace, PyArrow, lxml, pytest and test harness, plus an
actual XNYS session. Full commands/results are in PHASE_7_PLAN.

**Limits:** Validation uses stateful fake AWS/HTTP boundaries and network-disabled
containers, not live providers/resources. Container lifecycle is not a production
latency/deployment guarantee; shutdown grace still does not cap blocking I/O.
Provider selection and production universe remain unresolved. No shared-data
writes, AWS resource changes, live AWS/SEC/provider operations, downstream
relocation or Phase 8 work occurred.

### Phase 8 — Add CDK and application delivery

**Status: IMPLEMENTED (2026-10-09); live deployment not performed.**

The implementation, deployment gates and offline validation are recorded in
[PHASE_8_PLAN.md](PHASE_8_PLAN.md). The user authorized implementation; live
provisioning and activation remain separate. See [DEPLOYMENT.md](DEPLOYMENT.md).

Automatic ECS recovery is now decided in [ADR 008](adr/008-use-conservative-ecs-automatic-recovery.md):
stop-first replacement, 120s stop timeout, 150s SEC startup quiet period and no new
SEC request admissions on SIGTERM. This accepts conservative protection rather
than formal exclusion for automatic recovery. The 2.5-minute quiet period can
materially delay earnings ingestion and must be revisited using recovery metrics;
see [BACKLOG.md, ARCH-001](BACKLOG.md#arch-001--reduce-recovery-delay-during-active-earnings-windows).
The policy is implemented and tested offline; it has not been deployed.

**Goal:** Provision the approved infrastructure and automate tested application
image delivery while keeping initial infrastructure deployment manual.

**Major modules/files:** `infra/cdk/{app,ingestion_stack}.py`, `cdk.json`, CDK tests;
`.github/workflows/{ci,deploy}.yml`; deployment/runbook documentation.

**Existing code expected to be reused:** Phase 7 container/runtime, configuration
contracts, repository schema/access patterns, and observability definitions.
There is no legacy cloud infrastructure to reuse.

**New functionality:** ECR, ECS/Fargate cluster/service/task, S3 bucket, DynamoDB
tables/indexes, SNS topic, IAM policies, CloudWatch logs/alarms, and operational
DLQ. Use an LLD-approved secret configuration mechanism if the provider requires
credentials. Add GitHub OIDC authentication, identifiable image tags, ECS updates,
and deployment-success verification; keep CDK application separate from image
delivery even though both live in this repository.

**Testing expectations:** CDK assertions cover least privilege, retention and
immutable-write protection, recovery indexes, alarms, absence of public inbound
API, and single-active-task deployment. CI runs offline application tests and
image builds. Any live SEC smoke test remains explicitly manual.

**Acceptance criteria:**

- Infrastructure stays within the approved architecture; SNS is ingestion-owned,
  while consumer SQS subscription queues stay in consumer repositories/stacks.
- Permissions/policies prevent the ingestion role from deleting or unconditionally
  replacing raw objects; durable data is retained across infrastructure changes.
- Deployment preserves one active ingestion process. `desiredCount=1` alone does
  not prevent rolling-update overlap; stop the old task before starting its
  replacement and accept the short v0 deployment interruption.
- GitHub Actions fails on test/build/deployment failure, pushes an identifiable
  image, updates the ECS task/service, and verifies deployment success.
- CDK deployment remains manual initially; deployment instructions and alarms are
  documented and tested. No unapproved distributed coordination is introduced.

**Delivered:** Pinned Python CDK/Node toolchain, retained state/runtime stacks,
four contract-compatible tables/GSIs, conditional immutable S3 policies, encrypted
SNS/SQS, retained ECR, scoped task/execution/OIDC release roles, ARM64 Fargate at
count zero and eleven health/error/calendar/backlog/latency alarms. SNS uses a
dedicated retained KMS key for precisely scoped encrypted publisher access. ECR
lives in retained state so an image can be pushed before runtime deployment.

Runtime adds configurable monotonic SEC startup quiet timing, deployed metrics
environment and a shutdown admission guard across retries/redirects/limiter waits.
No domain/event/repository schema changes. Docker declares non-root writable `/tmp`
for ECS bind-mount copy-up. GitHub workflows run offline application/CDK/workflow/
ARM64 container validation; delivery uses immutable commit/run tags and resolved
digests, explicit STOPPED handoff, actual revision/digest/health verification and
bounded restoration that never turns a failed release into success. Stopped
environments and explicit activation gates are preserved.

**Validation (2026-10-09):** Final commands/results are in PHASE_8_PLAN. Offline
application, infrastructure/workflow and six actual network-disabled container
lifecycle checks pass, along with strict credential-free/no-lookup synthesis,
compilation, actionlint, distribution build and non-root volume probe. No live
AWS/SEC/provider calls, resource provisioning, company seeding, shared-data writes
or GitHub workflow execution occurred.

**Limits:** Offline synthesis/assertions are not live IAM/network/ECS verification.
Automatic replacement retains ADR 008's conservative overlap limitation and
150-second latency cost. Target subnet/AZ availability, notifications and memory
sizing need authorized cloud checks. Provider/universe remain unresolved and
production activation is blocked until they are supplied. CDK always deploys the
service stopped; manual runtime changes reconcile image/baseline and explicitly
reactivate. Public-subnet egress is v0's supported topology; private egress
requires a reviewed change. Terminal redrive and completed-package rechecking
remain deferred.

## 4. Current migration status

**Phases 1–4: COMPLETE. Phase 5: COMPLETE for authorized placeholder-provider scope.
Phases 6–7: COMPLETE. Phase 8: IMPLEMENTED and validated offline; not deployed.**

Phase 1 delivered typed contracts, deterministic identities, UTC timestamp
validation, offline submissions parsing, and configuration validation while
preserving the legacy CLI. Its historical validation was 224 tests, including
144 legacy tests.

Phase 2 delivered:

- Synchronous `SecClient` with reused sessions and an explicitly shared limiter.
- Serialized dispatch, conservative pacing, and rolling-window enforcement for
  submissions, indexes, downloads, retries, and manually followed redirects.
- Validated timeout/retry/redirect configuration, typed errors, bounded jittered
  retries, per-attempt failure logs, and recovery logs.
- Fresh responses with no permanent JSON cache.
- Pure HTML/directory package reconciliation, complete observed-file enumeration,
  primary resolution, available document types, safe links/names, and explicit
  incomplete-package failures.
- Exact response-content bytes and deterministic artifact construction.
- Offline unit tests and synthetic SEC-shaped package replay, including concurrent
  mixed operations and delayed package availability. These fixtures are synthetic,
  not represented as captured historical SEC responses.
- README, LLD, and example environment updates; no legacy code removal, AWS calls,
  scheduler/runtime, or shared-data changes.

**Phase 2 implementation choices:** Reused `requests` and BeautifulSoup; no dependencies
were added. One in-flight HTTP attempt avoids unsafe concurrent session access and
keeps dispatch admission atomic. The future async runtime must use a bounded
executor. Slow responses can reduce throughput; this implementation does not
promise the five-request/second maximum will always be achieved. Package
completeness means a consistent observed snapshot, not proof against later SEC
additions. Incomplete snapshots raise explicitly for caller-driven retry.

**Historical Phase 2 validation (2026-10-08):** 270 tests passed, including the existing legacy suite;
compilation and diff checks pass. Tests use fake transports/clocks and block live
network access in ingestion unit/replay tests. No live SEC or AWS validation was
performed. Phase 2 did not change the approved architecture; concrete interface
adjustments and limitations are recorded in LLD section 2.2.

Phase 3 delivered durable repositories, enumeration/checkpoint coordination,
bounded paginated/indexed recovery, isolated DynamoDB configuration, Boto3 adapters,
and 70 offline unit/integration tests. See the Phase 3 section and LLD sections
2.3/6.5/6.6 for concrete contracts and limitations. Historical Phase 3 validation: 340 tests
pass, compilation/diff checks pass; no live AWS or SEC operations were performed.

Phase 4 delivered the S3/SNS/SQS adapters, restart-safe discovery/acquisition workers,
durable stage budgets/terminal checkpoints and explicit paginated recovery passes.
See Phase 4 above and LLD sections 2.4/6.7 for contracts and limitations.

**Historical Phase 4 validation (2026-10-08):** 414 tests pass (340 prior tests plus
74 new tests), using the repository-local Python 3.14 environment. Compilation
(`.venv/bin/python -m compileall -q src tests`) and `git diff --check` pass.
The installed Boto3/Botocore conditional-write model is 1.43.110. All integration
boundaries are mocked and network-blocked. No live AWS/SEC operations, shared-data
changes or legacy removals occurred. No container jobs or infrastructure deployment
were run; production runtime/container cutover remains Phase 7.

Phase 5 delivered provider-independent complete-snapshot synchronization, safe
scoped reconciliation, durable monotonic sync/freshness metadata and an explicit
unconfigured placeholder. A selected live adapter was deferred at the user's
request. See Phase 5 above and LLD section 7 for contracts and limitations.
**Historical Phase 5 validation (2026-10-08):** 496 tests pass; compilation and diff
checks pass, using the repository-local environment and mocked offline boundaries.

Phase 6 delivered the continuous runtime and revised deterministic satisfaction
policy. See Phase 6 above and LLD sections 2.6/8.2 for concrete contracts and
limitations. **Historical Phase 6 validation (2026-10-08):** 583 offline tests pass;
compilation/diff checks and entry-point help/import checks pass. No live AWS/SEC/
provider operations, deployment, container jobs or shared-data writes were run.

Phase 7 completed package/container cutover and legacy cleanup. See its section
and LLD section 2.7 for delivered contracts. **Historical Phase 7 validation (2026-10-08):**
448 offline tests pass; six opt-in Docker lifecycle cases pass separately.
Compilation/diff checks, fresh distribution installation and image import/session/
dependency checks pass. Only offline fixture containers were run.

Phase 8 delivered CDK, scoped delivery roles, observability and controlled image
releases. See PHASE_8_PLAN.md for exact commands and DEPLOYMENT.md for the runbook.
**Current validation (2026-10-09):** 473 application tests and 15 infrastructure/
workflow tests pass; six opt-in ARM64 Docker lifecycle cases pass separately.
Strict credential-free synthesis, actionlint, distribution build, compilation,
heartbeat-volume probe and diff checks pass. No AWS resources were provisioned,
GitHub delivery executed or production inputs selected.

## 5. Next milestone

**NEXT: Resolve production inputs and separately authorize deployment/activation.**

Phase 8 implementation is delivered and validated offline. Initial CDK deployment
remains manual; no infrastructure has been provisioned. Follow DEPLOYMENT.md for
explicit account/network/OIDC configuration, staged infrastructure/image delivery,
live verification and activation gates. Implementation does not authorize live
cloud operations.

Select and implement the calendar adapter and approve the authoritative company
universe before production activation. The placeholder cannot supply calendar
coverage. Do not silently invent either input. Terminal operator redrive and
completed-package recheck policies remain explicit future work.

## 6. Remaining unresolved external inputs

| Input | Status | Required for |
| --- | --- | --- |
| Free earnings-calendar provider and available access | TBD; placeholder delivered by authorization | Deferred live provider adapter and production calendar use |
| Authoritative approximately 500-company universe with ticker/CIK/name/enabled fields | TBD | Production universe seeding; the legacy sample ticker list is insufficient |

**Neither input blocked Phases 3–4.** Use fixture companies and mocked SEC responses
for development. Do not invent a provider or silently choose a production universe.
Exact windows, safety cadence, retry constants, and alarm thresholds remain
configurable implementation choices within the approved design.

## 7. Progress conventions

After each phase:

1. Update this file's phase status and current-status section with what actually
   shipped; mark complete only when its acceptance criteria are satisfied.
2. Record material deviations from the original plan, their reasons, and affected
   contracts. Distinguish implementation choices from architectural changes.
3. Update HLD/LLD/ADRs when architecture changes, and README when commands,
   configuration, behavior, or package boundaries change.
4. Record validation commands/results, including test counts and any unresolved
   limitations. Do not present historical validation as a fresh run.
5. Clearly identify the next phase and update the next-milestone section so a
   future session can resume without conversation history.

Before work, inspect package/workspace instructions and the existing Git status.
Preserve unrelated user edits. Never interpret a dirty working tree as permission
to reset, remove, or commit those changes. Routine validation uses the repo-local
virtual environment and offline tests; generated shared data is not source code.
