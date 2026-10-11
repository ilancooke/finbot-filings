# SEC document acquisition architecture

This document describes the current service and its durable contracts. The POC
migration and subsequent earnings-filtering work are complete. Use
[README](../README.md) for installation, commands and settings,
[DECISIONS](DECISIONS.md) for rationale, [DEPLOYMENT](DEPLOYMENT.md) for operations,
and [BACKLOG](BACKLOG.md) for open work. Historical plans are recoverable in Git.

## Scope and ownership

The repository/distribution is `finbot-filings`; the installed Python namespace
is `finbot_ingestion`. It discovers SEC filings, selects relevant documents,
preserves original response-content bytes in S3, records durable facts in
DynamoDB and publishes metadata-only `ArtifactReady` events through SNS.

Initial coverage is the [50-company curated universe](PRODUCTION_UNIVERSE.md).
Approximately 500 companies is the capacity target, not the current company count.
CIK is stable company identity; ticker is lookup/convenience metadata. The runtime
requires an existing nonempty enabled universe and never provisions or seeds it.

Document interpretation, financial extraction, features, consumer queues and
portfolio/trading decisions belong downstream. Historical backfill tooling,
Company Facts, global EDGAR feeds, RAG, multiple ingestion tasks and distributed
rate limiting are outside the current service. This cloud producer does not use
the workspace's local Parquet/catalog pipeline or `FINBOT_DATA_ROOT`.

```mermaid
flowchart LR
    Yahoo[Yahoo calendar observations] --> Sync[Calendar sync]
    Sync --> DB[DynamoDB facts and checkpoints]
    DB --> Runtime[One Fargate ingestion task]
    SEC[SEC submissions and package metadata] --> Runtime
    Runtime --> S3[Immutable original documents in S3]
    S3 --> Stored[DynamoDB stored checkpoint]
    Stored --> SNS[SNS ArtifactReady]
    SNS --> Published[DynamoDB published checkpoint]
    SNS --> Consumers[Consumer-owned SQS queues]
    Runtime --> Failed[SQS failed work]
    Runtime --> Logs[CloudWatch logs, metrics and alarms]
```

## Filing admission and document selection

`selection/policy.py` implements deterministic `earnings-acquisition-v1` routing.
`LIVE_INGESTION_START_AT` defaults to **2026-10-10T00:00:00Z**, inclusive, using
SEC acceptance time. It is a fixed saved configuration value shared by restarts,
deployments and newly enabled companies. Calendar dates affect polling cadence,
not acquisition eligibility. Do not reset/widen the cutoff to perform backfill.

| Filing/document | Current rule |
| --- | --- |
| `8-K`, `8-K/A` | Require unambiguous Item 2.02 evidence |
| `10-Q`, `10-K`, and their `/A` amendments | Retain the primary financial report |
| Selected earnings 8-K | Retain primary plus identified earnings releases and financial supplements in HTML/text/PDF |
| Decks, standalone graphics/logos, complete submission TXT bundles, unrelated exhibits, auxiliary files | Exclude |
| Missing/conflicting item evidence or ambiguous exhibit role | Record an unresolved diagnostic; never assume relevance or irrelevance |

The submissions parser returns all six supported forms without a count cutoff;
admission happens separately. Identical accession duplicates collapse. Conflicts,
invalid relevant rows and invalid acceptance timestamps reject the response rather
than allowing a caller to checkpoint it as complete. Historical submissions-file
traversal is deferred; downtime recovery is limited to filings still present in
the recent response and already durable pending work.

Package discovery reconciles the HTML document table with directory JSON and
preserves descriptions/types. It resolves inline-viewer links, validates safe
accession paths and collapses compatible duplicate references. A missing primary
name resolves only when one document matches the filing form. Missing, empty or
inconsistent snapshots fail explicitly and are retried.

Descriptions and safe primary-report exhibit references determine release and
supplement roles; neither EX-99.1 nor EX-99.x alone proves eligibility. Missing
submissions items may be resolved from index metadata or structural primary
headings. Primary routing inspection is bounded to **4 MiB**; unsupported/larger
layouts remain unresolved. Clearly selected documents can proceed while ambiguous
attachments are skipped. Bounded text inspection may warn about image-only HTML;
there is no OCR, financial interpretation or fetching excluded attachments to
classify them. Legitimate selected `.txt` releases remain allowed; submission
bundles are excluded by accession-bundle identity. No linked images, stylesheets,
scripts or investor-relations content are acquired.

Fresh known exclusions create no work. Unresolved filing routing removes pending
enumeration eligibility through a selection checkpoint. Discovery and every
pending acquisition/publication path, including direct worker calls, use the same
policy. Published records and terminal/dead-letter obligations stay intact.
Completion covers selected documents, not preservation of the entire SEC package
or every earnings disclosure.

## Identity, time and public event contract

| Concept | Contract |
| --- | --- |
| Company | Ten-digit zero-padded CIK; reviewed ticker/name/enabled configuration |
| Filing | Dashed SEC accession number; amendments have new identities |
| Artifact | `<dashed-accession>/<original-filename>` |
| S3 key | `<10-digit-cik>/<artifact-id>` |
| `filed_at` | SEC `acceptanceDateTime`, normalized to aware UTC; not measured public availability |
| `discovered_at` | First canonical discovery observation |
| `stored_at` | Original S3 HEAD `LastModified`, including repair |
| `published_at` | First durable publication checkpoint |

Original filename case is preserved. Filenames must be safe basenames without
separators, traversal names or control characters. SEC URLs use numeric CIK and
dash-free accession path components. Missing/invalid/naive acceptance timestamps
fail; filing/report dates never substitute. S3 URI paths are percent-encoded;
consumers decode the URI path to obtain the actual key. No application content
hash or ETag-based identity is used. Original discovery/ticker facts remain
canonical on rediscovery, including after ticker configuration changes.

`ArtifactReady` is `artifact.ready` / schema `1.0`:

```json
{
  "event_type": "artifact.ready",
  "schema_version": "1.0",
  "artifact_id": "0001018724-26-012345/ex991.htm",
  "filing_id": "0001018724-26-012345",
  "cik": "0001018724",
  "ticker": "AMZN",
  "form_type": "8-K",
  "document_type": "EX-99.1",
  "filename": "ex991.htm",
  "s3_uri": "s3://example-artifacts/0001018724/0001018724-26-012345/ex991.htm",
  "filed_at": "2026-10-31T20:05:02Z",
  "discovered_at": "2026-10-31T20:05:08Z",
  "stored_at": "2026-10-31T20:05:09Z"
}
```

Events contain references, never document bytes/text. The worker validates matching
canonical filing/artifact metadata and paired storage facts before sending plain
JSON to the standard SNS topic. A MessageId is required before checkpointing
publication. Delivery is **at least once**: lost SNS/checkpoint acknowledgments
can repeat the same logical event. Consumers own their subscriptions/queues and
deduplicate by `artifact_id`. Consumer receipt is outside ingestion's checkpoint.

## DynamoDB schema and access patterns

Four separately configured tables are required. All base/index keys are strings;
fixed indexes use **KEYS_ONLY** projection.

| Table | Base partition / sort key | Index | Index partition / sort key |
| --- | --- | --- | --- |
| Companies | `cik` / none | `EnabledCompanies` | `enabled_marker` / `cik` |
| Calendar | `expected_date` / `cik` | None | — |
| Filings | `accession_number` / none | `PendingFilingEnumeration` | `pending_work_kind` / `pending_work_sort` |
| Artifacts | `artifact_id` / none | `PendingArtifactWork` | `pending_work_kind` / `pending_work_sort` |
| Artifacts | same | `ArtifactsByAccession` | `accession_number` / `filename` |

`repository_schema_version=1` is present on application records. Processing rows
start at numeric revision zero; guarded updates increment revision atomically.
Optional facts are omitted, not NULL. Persistence timestamps use fixed-width
`YYYY-MM-DDTHH:MM:SS.ffffffZ`; event JSON uses UTC ISO timestamps ending in `Z`.
Serialization validates numeric/identity fields, UTF-8 key limits (2048/1024 bytes)
and a conservative 400-KB item bound. Raw calendar diagnostics are finite JSON
encoded in `raw_provider_payload_json`; SDK AttributeValues never escape adapters.

| Record | Durable content |
| --- | --- |
| Company | Ticker/name/enabled plus `enabled_marker=ENABLED` only while enabled; configuration is mutable |
| Calendar expectation | Ticker/timing/provider, optional source ID/update time, `synced_at`, optional diagnostic JSON/replacement hint, `calendar_active` tombstone flag |
| Filing | Canonical CIK/ticker/form/acceptance/discovery/source/primary metadata; enumeration and processing checkpoints |
| Artifact | Canonical parent/CIK/ticker/form/type/filename/source/discovery metadata; paired `s3_uri`/`stored_at`, optional content type/byte count, publication and processing checkpoints |

Base-key reads and calendar date queries are strong. GSI queries are eventual;
candidates are hydrated through strong reads and rechecked before action. Calendar
ranges query each inclusive UTC date partition within the configured maximum span
(default 366 days). Sync and satisfaction records occupy reserved partitions:

| Partition (`expected_date`) | Sort key (`cik`) | Content |
| --- | --- | --- |
| `__calendar_sync__` | Provider | Typed revision-guarded latest run, separate full/near-term successes, sanitized failure facts |
| `__event_satisfaction__` | Canonical event identity JSON | Validated immutable match facts used to stop aggressive polling |

Sync records identify provider/kind, request ID, exact company/date scope,
observation/completion time, event/cancellation counts and failures. Repeating a
request ID reuses its canonical run; observation times increase across restarts
and clock rollback. Completion/failure requires the latest run. Compatible lost
acknowledgments preserve first completion; late failure cannot replace success.
Near-term success never replaces full success or hides another kind's failure.

Queries use scoped opaque SDK continuation tokens bound to region/query/table/
index/page size. There are no scans, FilterExpressions, TTL or recovery-age bounds
in normal application access. Continue empty actionable pages when a token remains.
Repeat whole passes to account for delayed index entries and inserted earlier work;
one empty pass cannot prove global completion. Low-cardinality work partitions
are a deliberate single-task choice, not a throughput or cost guarantee.

### Conditional creation and durable updates

Filing/artifact creation requires `attribute_not_exists(primary_key)`. A conflict
triggers a strong read: compatible rediscovery returns existing canonical facts;
incompatible identity/CIK/form/source provenance fails. Optional primary/type values
conflict when both are populated differently. Ticker/discovery updates never
replace first observations. Artifact creation accepts unprocessed discoveries.

Updates require existence plus matching revision, with bounded CAS retries that
reread and recompute. Missing records fail without phantom upserts. Compatible
repeated checkpoints preserve the first time; incompatible storage facts fail.
`mark_published` requires paired storage facts and removes pending eligibility.
Late failures cannot resurrect completed work. Repeated logical failure writes
reuse timestamp/error; equal matching or older observations do not count again,
and equal-time conflicts fail. Error text is bounded to 2048 characters. Legacy
retry counts are chronological diagnostics, not an exact concurrent audit ledger.

`PackageCheckpoint.persist()` validates canonical parent, resolved primary and
unique children, conditionally creates each child and strongly confirms durability
before marking enumeration complete. Known child identities are dispatched
directly rather than waiting for accession-index visibility. The first completed
snapshot retains timestamp, primary and positive child count. Explicit later
validated snapshots may add children; automatic completed-package rechecks are
deferred. Selection does not rewrite old snapshot counts/times. No package-wide
transaction or unbounded embedded manifest is used.

### Pending, selection and failure checkpoints

`pending_work_sort` is original fixed-width discovery time plus `/` and identity;
retries never move it. Pending keys change in the same write as durable progress:

| Durable facts | Pending work |
| --- | --- |
| Incomplete parent enumeration | `ENUMERATE` |
| Unstored artifact | `ACQUIRE` |
| Stored, unpublished artifact | `PUBLISH` |
| Terminal failure, send not checkpointed | `DEAD_LETTER` |
| Published/completed or dead-letter checkpointed | None |
| Selection `EXCLUDE` / `UNRESOLVED`, without terminal obligations | None |

Processing checkpoints add `selection_policy`, `selection_decision`
(`SELECT`/`EXCLUDE`/`UNRESOLVED`), `selection_reason` and `selection_checked_at`
together. `record_selection()` uses revision/existence guards. A skip invents no
storage, publication or terminal success/failure. Contradictory evidence can block
previous SELECT work; blocked work cannot automatically re-enter pending indexes.
Published/terminal records, including pending dead letters, remain untouched.

Enumeration/acquisition/publication have independent persisted workflow-failure
counters and paired latest stage/error/time facts. Immutable terminal facts contain
stage/error/type/time; optional `dead_lettered_at` records the first successful
operational send. Terminal stage must agree with unfinished work. Old rows may omit
additive fields; validators reject partial facts or inconsistent pending membership.
Older adapters cannot safely process newer terminal/calendar/selection records.

Terminal facts precede an `ingestion.work_failed` / `1.0` SQS envelope, containing
canonical work/source identifiers, stage, terminal time, error/type/count and
optional S3 reference, never bytes. Its stable `failure_id` is
`<filing|artifact>/<identity>/<stage>/<terminal-time>`. SQS MessageId precedes the
send checkpoint; lost acknowledgments can duplicate that failure ID. The ingestion
failed-work queue is separate from downstream queues/subscription DLQs.

Partially created children defer until parent enumeration completes; terminal
parents keep them deferred and supply the package failure envelope. Pagination
continues past deferred/failed work. Invalid metadata/storage conflicts terminalize
immediately; retryable source/SDK/CAS failures consume stage budgets. Shared
credential/permission/resource/configuration errors propagate rather than
terminalizing every company. Cancellation is never a workflow failure. Normal
workers never redrive terminal work; durable facts remain after queue retention.

## Storage, acquisition and repair

The ordered path is **S3 → stored checkpoint → strong reload → SNS → published
checkpoint**. When a record lacks storage facts, inspect S3 before downloading.
Compatible existing objects repair metadata without replacing bytes. Missing,
malformed or conflicting provenance fails explicitly; HEAD permission errors do
not mean absence.

S3 creation uses direct `PutObject(IfNoneMatch="*")`. Versioned ASCII JSON object
metadata `finbot-provenance` records identity, CIK, original ticker/form/type, SEC
URL and original discovery time; `finbot-content-type` records original type.
The provenance envelope is bounded to 1800 bytes, total user metadata to 2048
bytes and S3 keys to 1024 UTF-8 bytes. HEAD LastModified/ContentLength/type establish
stored facts for both initial writes and repair. A 412 reconciles existing content;
409/ambiguous writes inspect before bounded conditional retry. Only specific
missing-object 404 means absence; missing bucket/403 errors propagate. GetObject
and scoped ListBucket permissions support reliable absence detection. Ingestion
has no deletion or unconditional-overwrite permission.

Downloads stream original decoded response-content bytes, with no text transcoding
or interpretation. Selected acquisition has a maximum **64 MiB** per artifact,
configurable downward. Excess Content-Length or counted bytes fail explicitly;
there is no multipart fallback. Two whole artifact workflows run by default;
buffer conversion can temporarily retain two copies per workflow. Shared admission
slots bound primary routing bodies as well. Up to 32 package decision maps are
cached without bodies; eviction/restart rechecks metadata. Primary bytes fetched
for routing reuse the same immutable storage path rather than downloading twice.
Stored artifacts never download again for publication retries.

## SEC transport and runtime

Synchronous `SecClient` uses reused requests sessions behind one shared
`SECRateLimiter` and a bounded executor. Blocking HTTP/SDK calls never run on the
scheduler event loop. One SEC attempt is in flight; dispatch is serialized from
admission through response receipt, including streaming. Every poll, index,
download, retry and redirect consumes the same **five-request/second maximum**,
with pacing and a rolling-one-second guard. Lower configured ceilings apply.
Slow responses reduce throughput; maximum admission rate is not promised capacity.

HTTP retries/redirects are explicit and bounded; automatic transport retries and
redirects are disabled. Connection/timeouts, 403/429/5xx and package-index 404
receive centralized jitter/backoff and bounded Retry-After. Redirects stay on
official HTTPS SEC hosts. Successful source responses are never indefinitely
cached. All calls use an identifying SEC User-Agent. Public SEC methods are
`get_company_submissions`, its evidence-aware variant, `get_filing_index` and
`download_document`; async ingestion/repository interfaces are defined in source.

One RuntimeApplication shares repositories, identity locks, worker/recovery and
calendar sync. Default eleven supervised loops comprise scheduler, recovery,
refresh, reload, heartbeat, metrics, two poll workers, one enumeration worker and
two artifact workers. Pools/queues are bounded. Poll admission is nonblocking;
filing/artifact handoff has backpressure. Queued/in-flight identities suppress
duplicates. Recovery runs at startup and repeatedly after completion, independent
of company enabled status. Disabling a company stops polls, not durable recovery.
Coarse reloads hydrate universe/calendar/satisfaction; scheduler ticks do no
DynamoDB I/O. Due times use monotonic completion-based intervals with CIK-staggered
safety polls and no catch-up bursts. UTC wall time determines event windows.

Market windows use actual offline XNYS sessions, New York dates, DST, holidays and
early closes. Unknown timing gets a wider window; non-session expectations keep
their date with a configured fallback. Coverage errors are distinct from holidays.
Window caches are bounded and reload spans include past/grace/pre-window coverage.
Defaults and environment names are in [README](../README.md#calendar-scheduling-and-observability).

Required-loop failure, unexpected return or stalled busy work exits nonzero; idle
workers remain healthy. SIGTERM/SIGINT stops producers and new SEC/Yahoo admissions,
including paced attempts, retries and redirects. Queues drain within the configured
grace, then cancel cooperatively. Repeated cancellation cannot release admission
before actual blocking I/O finishes or close a client still in use. Drain grace is
not a hard total I/O deadline; forced termination relies on durable recovery.

### Earnings satisfaction is a separate scheduling policy

`earnings-satisfaction-v1` requires matching CIK and acceptance in
`[window_start, grace_end)`, plus one exact original form:

| Form | Required evidence | Saved reason |
| --- | --- | --- |
| `10-Q` | None | `original_10_q` |
| `10-K` | None | `original_10_k` |
| `8-K` | Unambiguous submissions Item 2.02 | `original_8_k_item_2_02` |

Amendments/generic or ambiguous 8-Ks cannot satisfy expectations. Admission may
use index/primary routing evidence; satisfaction never fabricates submissions
evidence from filenames, descriptions or contents. Candidates sort by acceptance
then accession; ambiguous overlapping expectations remain unsatisfied.

Persist satisfaction only after validating the durable filing and match facts:
accession/CIK/acceptance, expected date, window/grace bounds, reason/policy version,
original form and required normalized SEC items/evidence source. Event identity is
compact canonical JSON `[provider, CIK, "id", provider_event_id]`, or
`[provider, CIK, "date", expected_date]`, bounded to the 1024-byte sort-key limit.
Conditional creation and strong conflict/acknowledgment reads retain the first
compatible checkpoint; unknown policy versions fail explicitly. Satisfaction is
separate from mutable calendar rows and survives refresh/restart. Aggressive
polling stops only after durable satisfaction; safety polls/recovery continue.
It neither waits for publication nor proves extracted/validated earnings.

## Yahoo calendar synchronization

Provider selection is explicit: `yahoo` or default `placeholder`. Placeholder
raises `CalendarProviderNotConfigured`; it never invents an empty snapshot or
establishes freshness. Authoritative fixture providers retain omission-based
reconciliation; Yahoo uses replacement-only observations.

A CalendarSnapshot identifies provider, inclusive dates, exact sorted CIK scope,
normalized expectations and completeness. Freeze and paginate enabled companies;
empty/ambiguous universes fail. Validate full scope, limits, duplicates, timing and
finite diagnostics before expectation writes. Canonical ticker and monotonic
run observation time supply metadata. Failed/incomplete collection preserves
expectations and last successful freshness. Partial database application is not
atomic: success follows every required write, and a fresh validated snapshot
repairs interruption. No stored snapshot is replayed during provider outages.

Pinned `yfinance==1.7.0` / `curl_cffi==0.16.3` use an owned private `YfData.post`
seam; public DataFrames discard completeness evidence. Upgrade review must check
this boundary. Raw day slices validate query/schema/offset/total/count, terminal
empty pages and repeated slice consistency under aggregate HTTP/row/page/deadline
bounds. Each slice is enumerated twice; instability permits at most one bounded
restart. Request/auth/redirect/retry budgets are independent of SEC. New York
midnight bounds handle DST/adjacent overlap; D through D+29 uses upper query date
D+30. Curated ticker mapping must be unambiguous; BMO/AMC normalize to coarse
before/after-market, other codes to unknown. Financial extras are ignored.
Collection consistency does not establish independent company coverage or a
provider-wide atomic snapshot.

Only unique same-provider/CIK recognized quarterly-title hints
(`quarterly-announcement-v1/YYYY/Qn`) can link a date replacement. This optional
hint is not a provider ID or independently verified fiscal identity. Yahoo event
IDs/source update timestamps remain null. Missing hints, both dates present,
multiple candidates and out-of-confirmed-scope replacements preserve old events.
Known different quarterly hints at the same CIK/date reject a snapshot. Existing
incoming keys are excluded from old candidates so interrupted upserts can repair.
Strongly confirm the exact replacement before guarded cancellation of the unchanged
old observation. Disabled/other-provider/outside-range rows remain protected.

Calendar upserts accept absent or strictly newer observations; equal-time identical
repeats succeed, equal-time conflicts fail, older observations are ignored.
Cancellation retains fields, advances sync time and sets `calendar_active=False`.
Tombstones guard stale resurrection; old rows default active. Strong paginated date
reads omit tombstones. There is no physical deletion, TTL or tombstone compaction.
Date moves with Yahoo's null IDs require new satisfaction; old satisfaction is not
transferred to another date. Provider migration at occupied keys is deferred.

Refresh is completion-based, normally 30 inclusive days once daily with near-term
refresh disabled. Full and near-term success scopes remain separate. Restart may
reuse an unexpired daily success if company scope and scheduling coverage remain
sufficient, including across midnight; it must expose actual coverage rather than
claiming the new rolling tail was collected. Scope changes/insufficient coverage
trigger refresh. Full-success age and coverage determine freshness; near-term
success cannot hide stale full state.

Yahoo session, cookie/timezone SQLite caches and blocking work have one dedicated
owner thread/executor. Caches are private temporary state at `/tmp/finbot-yahoo`,
mode 0700, writable by the non-root runtime, never durable provenance or credentials
to log/commit. Stop closes admissions, awaits bounded I/O and closes resources on
their owner thread. Help/heartbeat checks neither initialize Yahoo nor contact it.
The provider-only diagnostic is documented in [DEPLOYMENT](DEPLOYMENT.md#one-shot-live-calendar-check).

## Observability, deployment and capacity limits

Sanitized JSON logs go to stderr; bounded thread-safe EMF goes to stdout with only
Service/Environment dimensions. CIK/accession/artifact IDs stay in logs. Metric
buffers cap samples/names and report drops. Operational samples can repeat during
repair and are not an exactly-once ledger. Discovery latency measures SEC acceptance
to first discovery; download latency is artifact discovery to storage; ingestion
latency is acceptance to storage. None measures public availability or consumer
completion. Atomic local heartbeat health is separate from calendar scope/age and
provider readiness; no inbound health API is exposed.

CDK owns retained state and a stopped-by-default ARM64 runtime. GitHub owns tested
application revisions through scoped OIDC; infrastructure remains manual. Keep
one active process, immutable digest releases and the explicit STOPPED handoff.
Automatic recovery uses min/max 0/100, 120-second stop timeout, 150-second SEC quiet
period and 300-second ECS health startup allowance. These reduce overlap risk
without formal cross-process exclusion and can delay earnings discovery materially.
[DEPLOYMENT](DEPLOYMENT.md) defines procedures; [DECISIONS](DECISIONS.md#adr-008)
records the tradeoff. Storage encryption follows [ADR 010](DECISIONS.md#adr-010).

Twenty-five companies polling every five seconds consume the entire SEC ceiling
before downloads, retries or safety work. Synthetic 500-company replay exercises
bounded queues, shared budgets and delayed disabled-company recovery; it is not a
production tail-latency, memory, billing or provider-reliability guarantee. FIFO
dispatch remains because replay did not justify extra fairness/cache machinery.
Historical numeric results from the original replay driver have been retired;
current source tests synchronize worker wait boundaries and include a slower-SDK
case. Reproduce offline with:

```bash
.venv/bin/python -m pytest -q -s tests/integration/test_phase6_replay.py
```

## Maintaining these contracts

Keep SDK/provider payloads behind adapters, identities/checkpoints deterministic,
history immutable and tests offline. Source protocols/domain validators are the
exact callable interfaces. Contract or architecture changes must update this
document and relevant decision; commands/settings belong in README/.env.example.
Use failure/restart tests for durable changes. Do not introduce new services,
multi-task coordination or downstream interpretation without an explicit scope
change. Tests, CI and release artifacts hold execution evidence; maintained docs
describe current behavior and material unresolved limits.
