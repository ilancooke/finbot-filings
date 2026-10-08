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
Legacy code, [README-legacy.md](../README-legacy.md), and the legacy roadmap are
implementation/reference material, not the specification for the new service.

The target Python namespace is `finbot_ingestion`. The physical repository and
distribution remain `finbot-filings` during migration; a repository rename is not
a prerequisite. Legacy `finbot_filings` temporarily coexists with the new package.

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

**Status: NEXT — not started.**

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

**Status: NOT STARTED.**

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

**Acceptance criteria:**

- Conditional writes produce one logical filing/artifact per identity without
  replacing original publication/discovery facts.
- A crash after filing creation or partial child creation leaves recoverable work.
  Enumeration completes only after all discovered child records are durable.
- Recovery uses explicit paginated/indexed access rather than unbounded scans;
  old incomplete work remains discoverable instead of silently aging out.
- Durable checkpoints drive restart inference; no `downloading` or per-queue-move
  state is persisted.
- Repository failure and regression tests pass; schema/access-pattern additions
  are documented before dependent services rely on them.

### Phase 4 — Deliver restart-safe acquisition and publication

**Status: NOT STARTED.**

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

- Raw objects are created conditionally (for example, `If-None-Match: *`) and
  never replaced on retry. Deterministic keys alone do not enforce immutability.
- If S3 succeeds before the metadata checkpoint, recovery inspects the existing
  object and repairs metadata without replacing bytes or inventing new provenance.
- Stored artifacts are not downloaded again merely because SNS publication fails.
- If SNS succeeds before `published_at` is saved, recovery may republish the same
  logical event. Document at-least-once delivery and consumer deduplication by
  `artifact_id`; do not claim exactly-once delivery.
- Restart finishes incomplete filing enumeration and artifact acquisition.
  Amendments retain separate immutable records and objects.
- Events contain references/metadata only and are published only after S3 and
  DynamoDB success. Completed records require no recovery action.
- Terminal failures before artifact creation are also representable; failed
  dead-letter sends do not silently discard work.
- Failure/restart integration tests and legacy regressions pass.

### Phase 5 — Add earnings-calendar synchronization

**Status: NOT STARTED.**

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

- Provider-specific fields remain within the adapter; other components receive
  normalized expectations and optional raw diagnostic payloads.
- A date move does not leave an obsolete active row under a date-based key.
  Reconcile only the range for which provider synchronization succeeded.
- Failed/incomplete fetches cannot erase valid durable expectations; existing
  records remain usable through provider outages.
- Record the last successful sync durably and make stale synchronization observable.
- Calendar synchronization tests and regressions pass. Calendar data remains
  mutable planning data, separate from immutable filing history.

### Phase 6 — Add scheduling and the continuous runtime

**Status: NOT STARTED.**

**Goal:** Coordinate calendar-driven polling, safety polling, acquisition, and
recovery in one supervised long-running process.

**Major modules/files:** `scheduler/{window_policy,polling_scheduler}.py`;
`ingestion/work_queue.py`; `main.py`; `observability/{logging,metrics,health}.py`;
runtime configuration; fake-clock scheduler and load replay tests.

**Existing code expected to be reused:** Completed SEC, persistence, acquisition,
recovery, and calendar services. Reuse the centralized retry policy and typed
contracts rather than implementing independent limits in scheduler/workers.

**New functionality:** Centralized market-time/window policy; configurable active,
safety, and grace schedules; in-memory `asyncio.Queue` and duplicate-work
suppression; worker supervision; coarse calendar reloads; durable expected-event
satisfaction (such as matched accession); startup reconstruction; SIGTERM handling;
structured logs, latency/error/health metrics, and calendar freshness tracking.

**Testing expectations:** Fake-clock tests cover active/safety boundaries,
daylight-saving changes, nontrading days, unknown report times, satisfied events,
grace periods, and restart. A roughly 500-company replay measures queue delay and
discovery/download contention. Test task failure, health reporting, and shutdown.

**Acceptance criteria:**

- Schedules/queues remain in memory; calendar/recovery state reloads from durable
  records. No DynamoDB read occurs on each scheduler tick.
- Every outbound SEC request still obeys the shared budget; duplicate due work
  does not grow the queue without bound.
- Aggressive polling stops for a satisfied expected event and remains stopped
  after restart; safety polling continues according to policy.
- Failed background tasks cannot leave a deceptively healthy idle process.
- SIGTERM stops new work and permits reasonable in-flight cleanup; interrupted
  work remains recoverable.
- Replay reports capacity/latency behavior. Twenty-five active companies polling
  every five seconds already consume the entire five-request/second budget before
  downloads or retries; do not promise unconditional downstream latency targets.
- Add elaborate fairness only if replay demonstrates starvation; retain a simple
  design otherwise. Runtime, scheduler, and recovery tests pass.

### Phase 7 — Complete runtime cutover and clean the package

**Status: NOT STARTED.**

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

- The supported entry point is `python -m finbot_ingestion.main`.
- Container lifecycle tests pass; image configuration and documented commands agree.
- No ingestion dependency remains on PyArrow, legacy interpretation modules,
  local dataset roots, content hashes, or raw overwrite flags. Retain only
  dependencies justified by acquisition (for example an index HTML parser).
- Existing shared data remains untouched; useful legacy code is recoverable.
- Downstream code relocation is not silently undertaken in other repositories.
- Obsolete docs/commands are removed or clearly archived, and the final package
  is focused on ingestion.

### Phase 8 — Add CDK and application delivery

**Status: NOT STARTED.**

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

## 4. Current migration status

**Phase 1: COMPLETE. Phases 2–8: NOT STARTED.**

Phase 1 delivered:

- Typed domain models.
- Deterministic filing/artifact identities and SEC URL construction.
- `ArtifactReady` serialization.
- Offline submissions parsing for all six supported forms.
- Strict UTC timestamp handling.
- Configuration validation capped at five SEC requests/second.
- Packaging, README, LLD, and example-configuration updates.
- Legacy CLI preserved.
- No AWS integration, network transport, scheduling, or runtime added.
- **224 tests passing**, including all 144 legacy tests.
- **Compilation and diff checks passing**; independent package imports and legacy
  CLI help also verified.

All twelve Phase 1 acceptance criteria in section 3 are satisfied. These are the
recorded implementation validation results, not a claim that tests were rerun for
this documentation-only handoff. No material architectural deviation was made in
Phase 1; concrete contract choices are recorded in LLD section 2.1.

**NEXT: Phase 2 — Implement shared SEC transport and complete package discovery.**

## 5. Next milestone

**NEXT: Phase 2 — Implement shared SEC transport and complete package discovery**

**Goal:** Build the SEC access layer and reliable complete-package discovery on
the Phase 1 contracts. Do not start AWS persistence or service orchestration.

**Starting scope:** `sec/client.py`, `sec/rate_limiter.py`, `sec/filing_index.py`,
`sec/errors.py`, centralized retry policy, transport configuration, and offline
unit/package replay fixtures. Reuse `sec/submissions.py`, `sec/urls.py`, domain
identities, legacy client policies, and generic accession-directory parsing.
Inspect current source/tests and design documents before editing.

**Important design note:** The requirement is a centralized SEC request budget
and reliable package discovery. Asynchronous HTTP is not an architectural
requirement. Synchronous or asynchronous transport is acceptable if its concurrency
behavior is justified, all outbound attempts share the budget, and it can fit the
later runtime without blocking scheduling. Document the selected approach and
any interface adjustment; do not assume a new HTTP library is required.

**Tests to implement:**

- Concurrent request scheduling with a fake monotonic clock across submissions,
  indexes, document downloads, retries, and followed redirects.
- Configured lower rates and the five-request/second ceiling, without bursts that
  violate a rolling one-second window.
- Identifying headers, reused connections, timeouts, retryable network/HTTP errors,
  bounded backoff/jitter, and failure/recovery logging.
- Repeated submissions and package reads returning changed data rather than
  permanent cached JSON.
- Complete package enumeration for non-XBRL filings, many exhibits, PDFs, duplicate
  links, missing primary names, unsafe names, and temporarily incomplete indexes.
- Exact response-byte preservation and stable artifact identity across retries.

**Milestone acceptance:** All SEC requests use one centralized budget; concurrency
never exceeds the configured ceiling or five starts in a rolling second; fresh
responses reveal new filings/documents; complete packages yield the primary and
all attached documents with original names/types where available and no duplicate
logical artifacts. There is no EX-99.1-only filter, XBRL prerequisite, content
interpretation, hashing, AWS integration, scheduling, or runtime addition. New
offline tests and legacy regressions pass, and the transport/interface choice is
documented. See the full Phase 2 section for the complete phase contract.

**Plan clarification:** The earlier proposed sequence suggested async transport.
The approved handoff explicitly makes HTTP transport style an implementation
choice; centralized rate control and reliable discovery remain mandatory. Phase 2
has not started as part of this documentation task.

## 6. Remaining unresolved external inputs

| Input | Status | Required for |
| --- | --- | --- |
| Free earnings-calendar provider and available access | TBD | Production provider adapter in Phase 5 |
| Authoritative approximately 500-company universe with ticker/CIK/name/enabled fields | TBD | Production universe seeding; the legacy sample ticker list is insufficient |

**Neither input blocks Phase 2.** Use fixture companies and mocked SEC responses
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
