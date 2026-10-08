# Low-Level Design — finbot SEC Document Ingestion Service

## 1. Purpose

This document translates the HLD into an implementation contract suitable for coding the v0 service.

Where this document says **TBD/configurable**, Codex should expose configuration and should not invent a permanent architectural rule.

## 2. Repository structure

Recommended layout:

```text
finbot-sec-ingestion/
├── src/
│   └── finbot_ingestion/
│       ├── __init__.py
│       ├── main.py
│       ├── config.py
│       ├── domain/
│       │   ├── company.py
│       │   ├── calendar.py
│       │   ├── filing.py
│       │   ├── artifact.py
│       │   └── events.py
│       ├── calendar/
│       │   ├── provider.py
│       │   ├── service.py
│       │   └── providers/
│       │       └── <selected_provider>.py
│       ├── scheduler/
│       │   └── polling_scheduler.py
│       ├── sec/
│       │   ├── client.py
│       │   ├── rate_limiter.py
│       │   ├── submissions.py
│       │   └── filing_index.py
│       ├── ingestion/
│       │   ├── discovery_service.py
│       │   ├── artifact_downloader.py
│       │   └── ingestion_worker.py
│       ├── repositories/
│       │   ├── filing_repository.py
│       │   ├── artifact_repository.py
│       │   ├── calendar_repository.py
│       │   └── dynamodb/
│       ├── storage/
│       │   └── s3_artifact_store.py
│       ├── messaging/
│       │   └── sns_publisher.py
│       └── observability/
│           ├── logging.py
│           └── metrics.py
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── replay/
│   └── fixtures/
├── infra/
│   └── cdk/
├── docs/
│   ├── HLD.md
│   ├── LLD.md
│   └── adr/
├── .github/
│   └── workflows/
│       └── deploy.yml
├── Dockerfile
├── pyproject.toml
├── .env.example
└── README.md
```

The exact file names may evolve, but ownership boundaries should remain explicit.

### 2.1 Phase 1 implementation notes

The current repository ships `finbot_ingestion` alongside the unchanged legacy
`finbot_filings` namespace. Only domain contracts, SEC URL/submissions adapters,
and SEC configuration validation are implemented; the remaining layout describes
later phases. Models use frozen Python dataclasses and aware UTC datetimes.

Concrete Phase 1 contract choices:

- CIKs normalize to ten-digit strings. Filing identities retain dashed accession
  numbers; only SEC archive URL path components remove the dashes.
- Artifact IDs are `<accession_number>/<filename>`. `Artifact.artifact_id` is
  computed, not a caller-supplied field. Original filename case is preserved;
  basenames cannot contain separators, traversal names, or control characters.
- `filed_at` is sourced from SEC `acceptanceDateTime`, normalized to UTC. It is
  an acceptance timestamp, not an independent measurement of public availability.
  Missing, malformed, or naive timestamps raise `SECDataError`; filing/report
  dates are never substituted. Optional missing primary-document names remain
  `None` pending package enumeration.
- The pure parser returns all six relevant forms, newest-first, without fetching
  historical submissions. Identical accession duplicates collapse; conflicting
  duplicates and malformed relevant rows raise `SECDataError`, preventing a
  caller from checkpointing the response as complete.
- Artifact storage fields `s3_uri` and `stored_at` must be set together;
  `published_at` requires storage. No transient status or content hash is added.
- `ArtifactReady.from_artifact()` validates matching filing/artifact metadata and
  populated storage fields. Serialization uses the section 5 field set and UTC
  ISO timestamps ending in `Z`. Actual storage/metadata commit verification and
  publication ordering remain responsibilities of the future ingestion worker.
- `IngestionConfig.from_env()` reads process environment or an injected mapping,
  without implicitly reading `.env`. In Phase 1 it validates `SEC_USER_AGENT`
  and a finite SEC request ceiling in `(0, 5]`, defaulting to `5`. AWS/calendar
  settings and actual request-budget enforcement are implemented in later phases.

### 2.2 Phase 2 implementation notes

`sec/client.py` now implements synchronous `SecClient` using the existing
`requests` dependency. Its public contracts are:

```python
get_company_submissions(company: Company, *, discovered_at=None) -> list[Filing]
get_filing_index(filing: Filing) -> FilingIndex
download_document(url: str) -> DownloadedDocument
```

These replace the illustrative async SEC signatures below for the current
implementation. Provider payloads stay inside the client/pure parsers. The future
async runtime must use a bounded executor; it must not call blocking HTTP on the
scheduler event loop. No runtime/executor orchestration is added in Phase 2.

The caller explicitly supplies one shared `SECRateLimiter`. All clients using it
share a dispatch lock held from admission through HTTP response receipt. Requests
are paced without burst credit, with an additional rolling-one-second guard;
configured lower rates impose longer spacing. One in-flight attempt is an accepted
v0 tradeoff: slow responses reduce throughput. Backoff occurs outside the lock.
Sessions are reused, automatic HTTP retries/redirects are disabled, and every manual
retry/redirect consumes the same budget. Clients are context managers; closing one
closes its session, not the shared limiter. Successful responses are never cached.

`ingestion/retry_policy.py` centralizes bounded exponential full-jitter timing.
Connection/timeouts, 403/429/5xx and package metadata 404 are retried; other HTTP
errors fail immediately. Redirect chains have a separate finite hop bound.
Retry-After supports seconds/dates and is bounded by the configured delay cap.
Failures and successful recovery emit logging records with operation/attempt/error
fields and company/accession context when available. Logging timestamps come from
the standard LogRecord; production JSON formatting remains Phase 6.

`sec/filing_index.py` reconciles HTML document tables and directory JSON. It
validates accession paths and filenames, resolves inline-viewer document links,
preserves document types when present, collapses duplicate references, rejects
conflicting types, and enumerates all directory files except recognized index
infrastructure. No extension/form/exhibit-specific acquisition cutoff is used.
Missing primary names are resolved only when one document has the filing form.
`FilingIndex.documents` is a deterministic filename-sorted tuple; `artifacts()`
constructs domain artifacts using an explicitly supplied discovery timestamp.

Empty, missing, or inconsistent package metadata raises
`SECIncompletePackageError`, a `SECDataError` subtype. The caller must retry the
whole fresh snapshot later and must not mark enumeration complete. Malformed or
unsafe metadata also fails explicitly. Successful enumeration validates the
observed snapshot; it cannot prove no future documents will appear. Durable child
creation, enumeration checkpoints, and subsequent reconciliation remain Phase 3+.
The Phase 1 `sec.submissions.SECDataError` import remains compatible.

`DownloadedDocument` carries response-content bytes, content type, final source
URL, and computed byte length. HTTP content decoding follows requests semantics;
there is no text transcoding, document parsing, hashing, or storage.

Configuration adds positive finite connection/read timeouts (10/30 seconds),
positive maximum attempts (3), positive base/cap backoff (1/30 seconds), and a
nonnegative redirect limit (5). See README for environment names.

### 2.3 Phase 3 implementation notes

Async repository protocols now have DynamoDB adapters for companies, calendar
expectations, filings, and artifacts. The four-table contract below is concrete;
resource creation/CDK remains Phase 8. Boto3 is the only added runtime dependency.
No S3/SNS clients, discovery service, recovery orchestrator, calendar provider,
scheduler, or main runtime are introduced.

The low-level SDK client is deliberately used instead of shared Boto3 resources:
the client supports bounded executor use and direct Stubber validation, whereas
resources/sessions are not thread-safe. AttributeValue conversion stays inside
`repositories/dynamodb/serialization.py`. `DynamoDBExecution` shares the client,
limits executor admission, awaits in-flight SDK calls on cancellation, and has
explicit close/context-manager lifecycle. One execution belongs to one event loop;
close it after repository work finishes. Client construction is explicit through
`from_config()` or injection; importing modules/configuration does not create one.

Persistence configuration is a separate `DynamoDBConfig`; Phase 1–2 SEC settings
retain their original requirements. See README for exact environment names.
Standard SDK retries have finite total attempts/timeouts. Conditional compare-and-
set retries have a separate finite limit and recompute against durable facts.
Other SDK errors propagate; they are not converted into missing records or success.

`FilingCheckpoint` holds enumeration and failure facts separately from immutable
`Filing`. Existing domain/event fields and identity rules are unchanged. Timestamp
storage uses `YYYY-MM-DDTHH:MM:SS.ffffffZ`, supporting chronological string ordering
including subsecond differences. Event serialization retains its original format.
Schema version `1`, omitted optional fields (rather than NULL checkpoints), strict
integer conversion, validated identities, and a conservative 400-KB item-size
guard make malformed persistence explicit. Calendar raw payloads use a reversible
JSON string, accepting finite JSON floats without leaking DynamoDB Decimal types.
UTF-8 partition/sort-key limits (2048/1024 bytes) are checked without truncating
identities; unusually long source filenames fail explicitly at persistence.

`PackageCheckpoint.persist(filing, artifacts, primary_document_name=...,
completed_at=...)` operates on typed validated snapshots, not raw SEC metadata.
It validates the canonical parent, primary membership and unique child identities,
creates/rechecks each child, and only then calls `mark_enumerated()`. GSI results
never prove child durability. Newly created children use the original parent ticker
so `ArtifactReady.from_artifact()` keeps its matching-metadata requirement even
after a ticker alias changes. Child records created independently with a conflicting
parent ticker fail the helper explicitly rather than silently breaking event creation.

The first completed snapshot's timestamp, primary name and count are retained.
Later validated snapshots may idempotently add children through this helper;
automatic reconciliation cadence remains later orchestration. No package-wide
transaction or embedded unbounded manifest is needed: children are immutable,
application repositories do not delete them, and incomplete parents stay indexed.

### 2.4 Phase 4 implementation notes

The repository now ships async discovery, acquisition/publication and explicit
recovery services with low-level S3/SNS/SQS adapters. The event contract remains
`artifact.ready` / `1.0`. No calendar provider, production scheduler/queue,
continuous recovery loop, main runtime, resource provisioning or deployment is
introduced. The legacy package and shared data are preserved.

Concrete service contracts:

```python
DiscoveryService.discover(company: Company) -> tuple[str, ...]  # accessions
DiscoveryService.enumerate_filing(accession_number: str) -> EnumerationResult
ArtifactDownloader.acquire(artifact: Artifact) -> StoredArtifact
IngestionWorker.discover_company(company: Company) -> tuple[str, ...]
IngestionWorker.process_filing(accession_number: str) -> Outcome
IngestionWorker.process_artifact(artifact_id: str) -> Outcome
IngestionWorker.send_dead_letter(kind: str, identity: str) -> Outcome
RecoveryService.run_pass() -> RecoverySummary
```

`Outcome` is `complete`, `deferred` or `terminal`; `EnumerationResult` additionally
contains known artifact IDs. A completed filing outcome describes enumeration,
not proof all its previously created children have published. RecoverySummary
counts candidates/outcomes/errors in one observed pass, without an unbounded list
of results. A recovery candidate can be deferred by an unfinished/terminal parent.

Discovery conditionally creates filings, reloads canonical metadata and finishes
unfinished enumeration through PackageCheckpoint. New children are dispatched by
known identities and strong reads rather than relying on instant accession-GSI
visibility. Original ticker and first discovery timestamps remain canonical.
An existing completed accession does not automatically trigger a package recheck;
explicit later validated snapshots can still add children through PackageCheckpoint.
Submissions fetch/parse errors propagate without a success watermark or invented
accession. A later company poll retries that discovery.

`BlockingExecution` is shared by SEC and SDK offloading. `AWSExecution` owns an
explicit injected/constructed SDK client and finite standard retry/timeouts;
DynamoDBExecution reuses it. Executor admission survives repeated cancellation
until the actual blocking call completes. Closing with active work fails explicitly.
AWS needs-retry hooks log each failed HTTP attempt and recovered requests without
wire bodies/credentials. Workflow/checkpoint/DLQ failures also log contextual IDs.

Share one discovery/worker and IdentityLocks registry across callers, one SEC
client/limiter and one SEC executor. Locks are scoped by filing/artifact identity
and removed after use. The worker holds a configurable artifact admission slot
across the whole acquisition/publication path; this bounds retained document bytes,
not only SDK request concurrency. There is no distributed coordination.

Storage uses direct `PutObject(IfNoneMatch="*")` under the original filename key.
It inspects before any SEC download for a record lacking a storage checkpoint.
Object metadata includes an ASCII JSON envelope (`finbot-provenance`, version 1)
of artifact identity, CIK, original ticker/form/document type, canonical SEC URL
and original discovered_at, plus `finbot-content-type`. The provenance header has
an 1800-byte bound; total user metadata is limited to 2048 bytes. Keys are bounded
to 1024 UTF-8 bytes. Existing missing/malformed/conflicting metadata fails without
rewriting the object. S3 URI paths are percent-encoded; actual keys retain original
characters, and downstream readers decode the URI path.

Both initial creation and repair use HEAD LastModified for stored_at, ContentLength
for size_bytes, and the recorded original content type. Restart time is never
substituted. HTTP 412 reconciles the existing object; 409 retries conditionally
after inspection. Ambiguous write outcomes also inspect before retry. Only a
specific 404 missing-object response means absence; 403 and missing-bucket errors
propagate. Reliable missing-object detection requires GetObject and scoped
ListBucket, in addition to conditional PutObject permissions. Delete/unconditional
overwrite permissions are outside the ingestion contract.

`download_document(url, max_bytes=...)` streams under the same dispatch lock and
budget, rejects excessive Content-Length, counts decoded response-content chunks,
and closes the response on every path. Phase 4 defaults to a 64-MiB maximum per
artifact, configurable downward, with two whole artifact workflows in flight.
Buffer-to-bytes conversion temporarily retains both copies. No unsafe multipart
fallback, application content hash or interpretation is added. The older unbounded
`download_document(url)` signature remains compatible for existing direct callers.

The worker performs S3 → mark_stored → strong reload → SNS → mark_published.
Stored work never downloads on publication retry. After ambiguous database
acknowledgments, it reloads durable facts before repeating external calls or counting
failure. SNS sends plain ArtifactReady JSON and requires a MessageId. SNS or SDK
retries can duplicate the same logical event; consumers deduplicate by artifact_id.
This is at-least-once delivery, not exactly once.

Configuration remains isolated: AWSIOConfig, StorageConfig, MessagingConfig and
WorkflowConfig read environment/injected mappings with no client creation. See
README/.env.example for names/defaults. Standard SNS topic and standard operational
SQS queue must match AWS_REGION. Existing SEC/legacy configuration requirements
are unchanged. Boto3's minimum is 1.43.110, the verified conditional-PUT model.

## 3. Core domain models

Use typed Python models/dataclasses/Pydantic models as appropriate. Avoid exposing raw provider/SEC response structures outside adapters.

### 3.1 Company

```python
Company(
    ticker: str,
    cik: str,
    name: str,
    enabled: bool,
)
```

Requirements:

- CIK stored in normalized zero-padded string form where needed for SEC URLs/contracts.
- ticker is convenience metadata, not the primary SEC identity.

### 3.2 ExpectedEarningsEvent

Represents a calendar expectation, not an actual filing.

```python
ExpectedEarningsEvent(
    company_cik: str,
    ticker: str,
    expected_date: date,
    time_of_day: str | None,   # before_market, after_market, unknown, provider-specific mapped value
    provider: str,
    provider_event_id: str | None,
    provider_updated_at: datetime | None,
    synced_at: datetime,
    raw_provider_payload: dict | None,
)
```

The provider abstraction must normalize external fields into this model.

### 3.3 Filing

```python
Filing(
    accession_number: str,
    company_cik: str,
    ticker: str,
    form_type: str,
    filed_at: datetime,
    discovered_at: datetime,
    filing_index_url: str,
    primary_document_name: str | None,
)
```

Identity: `accession_number`.

### 3.4 Artifact

```python
Artifact(
    artifact_id: str,
    accession_number: str,
    company_cik: str,
    ticker: str,
    form_type: str,
    document_type: str | None,
    filename: str,
    sec_url: str,
    s3_uri: str | None,
    content_type: str | None,
    size_bytes: int | None,
    discovered_at: datetime,
    stored_at: datetime | None,
    published_at: datetime | None,
    retry_count: int,
    last_error: str | None,
    last_error_at: datetime | None,
)
```

Identity rule:

```text
artifact_id = deterministic(accession_number, filename)
```

No content hash in v0.

## 4. Key interfaces

Interfaces should make infrastructure/provider-specific code replaceable and unit-testable.

### 4.1 EarningsCalendarProvider

```python
class EarningsCalendarProvider(Protocol):
    async def fetch_events(
        self,
        start_date: date,
        end_date: date,
        companies: Sequence[Company],
    ) -> list[ExpectedEarningsEvent]: ...
```

Provider selection is TBD. No other package should depend on provider-specific field names.

### 4.2 CalendarRepository

```python
class CalendarRepository(Protocol):
    async def upsert_events(self, events: Sequence[ExpectedEarningsEvent]) -> None: ...
    async def get_events(self, start: datetime, end: datetime) -> list[ExpectedEarningsEvent]: ...
```

### 4.3 SecClient

All SEC HTTP traffic must pass through this component.

```python
class SecClient:
    async def get_company_submissions(self, cik: str) -> SecSubmissionsResponse: ...
    async def get_filing_index(self, filing: Filing) -> FilingIndex: ...
    async def download_document(self, url: str) -> DownloadedDocument: ...
```

The client owns:

- User-Agent/header policy;
- connection reuse;
- global request limiter integration;
- HTTP retries/backoff where appropriate;
- SEC-specific error translation.

### 4.4 FilingRepository

```python
class FilingRepository(Protocol):
    async def exists(self, accession_number: str) -> bool: ...
    async def create_if_absent(self, filing: Filing) -> bool: ...
    async def get(self, accession_number: str) -> Filing | None: ...
```

`create_if_absent` must be implemented atomically using DynamoDB conditional writes.

### 4.5 ArtifactRepository

```python
class ArtifactRepository(Protocol):
    async def create_if_absent(self, artifact: Artifact) -> bool: ...
    async def get(self, artifact_id: str) -> Artifact | None: ...
    async def mark_stored(
        self,
        artifact_id: str,
        s3_uri: str,
        stored_at: datetime,
        content_type: str | None,
        size_bytes: int | None,
    ) -> None: ...
    async def mark_published(self, artifact_id: str, published_at: datetime) -> None: ...
    async def record_failure(self, artifact_id: str, error: str, at: datetime) -> None: ...
```

Do not implement fast-changing statuses such as `downloading` in DynamoDB.

### 4.6 ArtifactStore

```python
class ArtifactStore(Protocol):
    async def inspect(self, artifact: Artifact) -> StoredArtifact | None: ...
    async def put_if_absent(
        self, artifact: Artifact, downloaded: DownloadedDocument,
    ) -> StoredArtifact: ...
```

S3 key format:

```text
<cik>/<accession_number>/<filename>
```

`StoredArtifact` contains s3_uri, stored_at, content_type and size_bytes. Conditional
creation, not the deterministic key alone, enforces safe retry. See section 2.4.

### 4.7 ArtifactEventPublisher

```python
class ArtifactEventPublisher(Protocol):
    async def publish_artifact_ready(self, event: ArtifactReady) -> None: ...
```

Implementation: SNS.

## 5. ArtifactReady contract

Version the contract from the beginning.

Example:

```json
{
  "event_type": "artifact.ready",
  "schema_version": "1.0",
  "artifact_id": "<deterministic-id>",
  "filing_id": "0001018724-26-012345",
  "cik": "0001018724",
  "ticker": "AMZN",
  "form_type": "8-K",
  "document_type": "EX-99.1",
  "filename": "ex991.htm",
  "s3_uri": "s3://finbot-sec-artifacts/0001018724/0001018724-26-012345/ex991.htm",
  "filed_at": "2026-10-31T20:05:02Z",
  "discovered_at": "2026-10-31T20:05:08Z",
  "stored_at": "2026-10-31T20:05:09Z"
}
```

Rules:

1. One event per stored artifact.
2. Publish only after S3 storage and DynamoDB metadata commit succeed.
3. Do not embed document bytes or full document text.
4. Downstream consumers fetch content from S3 using the artifact reference.
5. `artifact_id` is the idempotency key for consumers.
6. Downstream queues may apply SNS subscription filtering by form/document metadata later.

## 6. DynamoDB design

v0 may use separate tables or a single-table pattern. Prefer clarity over cleverness.

A straightforward multi-table layout is acceptable:

### 6.1 Companies table

Key:

```text
PK: cik
```

Attributes:

- ticker;
- name;
- enabled.

### 6.2 Earnings calendar table

Suggested key:

```text
PK: expected_date (YYYY-MM-DD)
SK: cik
```

Attributes:

- ticker;
- time_of_day;
- provider;
- provider_event_id;
- provider_updated_at;
- synced_at;
- raw_provider_payload (optional).

If later access patterns require efficient lookup by CIK, add a GSI rather than scanning.

### 6.3 Filings table

Key:

```text
PK: accession_number
```

Attributes:

- cik;
- ticker;
- form_type;
- filed_at;
- discovered_at;
- filing_index_url;
- primary_document_name.

Create using conditional write to guarantee idempotency.

### 6.4 Artifacts table

Key:

```text
PK: artifact_id
```

Attributes:

- accession_number;
- cik;
- ticker;
- form_type;
- document_type;
- filename;
- sec_url;
- s3_uri;
- content_type;
- size_bytes;
- discovered_at;
- stored_at;
- published_at;
- retry_count;
- last_error;
- last_error_at.

The concrete Phase 3 GSIs below support accession-child queries and recovery.
Company/time analytical indexes remain deferred until there is an access pattern.

### 6.5 Phase 3 concrete schema and access patterns

All base and GSI keys below have DynamoDB type `S`. All four table names are
configured separately. Index names are fixed adapter contracts for future CDK:

| Table | Base partition / sort key | GSI | Index partition / sort key | Projection |
| --- | --- | --- | --- | --- |
| Companies | `cik` / none | `EnabledCompanies` | `enabled_marker` / `cik` | KEYS_ONLY |
| Calendar | `expected_date` / `cik` | none | — | — |
| Filings | `accession_number` / none | `PendingFilingEnumeration` | `pending_work_kind` / `pending_work_sort` | KEYS_ONLY |
| Artifacts | `artifact_id` / none | `PendingArtifactWork` | `pending_work_kind` / `pending_work_sort` | KEYS_ONLY |
| Artifacts | same | `ArtifactsByAccession` | `accession_number` / `filename` | KEYS_ONLY |

`repository_schema_version=1` is present on every record. Filings/artifacts also
carry numeric `revision=0` initially. Successful guarded updates increment revision
with the durable facts in one atomic `UpdateItem`; no transient state is added.

Companies contain the section 6.1 fields and `enabled_marker="ENABLED"` only while
enabled. Upserts deliberately replace mutable company configuration. `list_enabled`
queries the sparse index, then strongly reads the base rows to discard stale entries.

Calendar contains section 6.2 fields; optional diagnostic payload is stored as
`raw_provider_payload_json`. Upserts are per-row conditional PutItem operations:
absent row or strictly newer `synced_at`. Equal-time identical repeats succeed
without replacement; equal-time conflicts raise; older observations are ignored.
No batch-wide atomicity is claimed. `get_events(start, end)` uses inclusive UTC
calendar dates containing the aware datetimes, queries each date partition, and
paginates it. The configured maximum date span defaults to 366 days. Moved-date/
cancellation reconciliation and last successful provider sync remain Phase 5.

Filings retain section 6.3 metadata and add:

- `retry_count`, paired optional `last_error`/`last_error_at`;
- paired enumeration completion facts: `enumeration_completed_at`,
  `resolved_primary_document_name`, `enumerated_artifact_count` (positive integer);
- `pending_work_kind="ENUMERATE"` and `pending_work_sort` until enumeration completes.

Artifacts retain section 6.4 metadata and add internal revision/pending attributes:

- no storage: `pending_work_kind="ACQUIRE"`;
- stored without publication: `pending_work_kind="PUBLISH"`;
- published: pending-work attributes are removed.

`pending_work_sort` is the fixed-width original discovery timestamp followed by
`/` and accession/artifact identity. It never advances on retries. Sparse keys
are created/transitioned/removed in the same write as their checkpoint. They
represent durable work eligibility, not queue membership or `downloading` state.
No TTL or discovery-age lower bound is used for recovery.

Concrete queries:

| Repository operation | API/access path | Consistency |
| --- | --- | --- |
| Company/filing/artifact get; filing checkpoint get | GetItem by full base key | Strong |
| Enabled-company page | Query EnabledCompanies, marker equality | Eventual; strong hydration |
| Calendar range | Query base expected_date equality for each selected day | Strong |
| Pending filing page | Query PendingFilingEnumeration, ENUMERATE equality | Eventual; strong hydration |
| Pending artifact page | Query PendingArtifactWork, ACQUIRE or PUBLISH equality | Eventual; strong hydration |
| Child artifact page | Query ArtifactsByAccession, accession equality | Eventual; strong hydration |

Queries use SDK paginators, no FilterExpression or Scan. `PageSize` and `MaxItems`
bound candidates per public Page (default 100, configurable to 1000). Tokens wrap
the SDK continuation with region/query/table/index/page-size scope; they are opaque to
callers, not raw LastEvaluatedKey dictionaries. Empty actionable pages can retain
a next token after stale/missing candidates are discarded. Continue to token None.
There is no snapshot isolation across query pages; callers must repeat complete
passes to pick up delayed GSI entries or work inserted earlier in the ordering.
Future workers should continue a pass rather than repeatedly restart at the oldest
failed row. A single empty pass cannot prove the absence of pending work.

The work-class partitions are deliberately low cardinality for v0's single
ingestion task. No throughput/cost guarantee is inferred from the SEC HTTP ceiling;
packages can fan out to many DynamoDB writes. Write sharding requires measured
need and a future additive index migration. PITR, deletion protection, retention,
capacity, encryption and monitoring are Phase 8 infrastructure decisions; Phase 3
does not create/change resources. Streams/outbox publication is not introduced;
the approved S3 → DynamoDB → SNS worker ordering remains Phase 4.

### 6.6 Conditional creation and checkpoint semantics

Filing/artifact PutItem requires `attribute_not_exists(primary_key)`. A conditional
failure triggers a strong read: compatible repeats return False; incompatible CIK,
form/source provenance, or artifact identity raises RepositoryConflict. Optional
primary/document types only conflict when both are populated differently. Later
ticker/discovery metadata never replaces original values. Artifact creation accepts
only unprocessed discovery records; checkpoints must use guarded updates.

Every processing update requires `attribute_exists(primary_key)` and matching
`revision`. Missing rows raise RepositoryNotFound, without phantom upserts. A
bounded CAS loop re-reads on conflicts and evaluates the intended transition anew;
exhaustion raises RepositoryBusy. ReturnValues ALL_NEW validates committed results.

`mark_stored` atomically writes URI/time/content metadata and PUBLISH eligibility.
Repeated matching URI/content-type/byte-count checkpoints preserve the first time;
incompatible metadata conflicts. `mark_published` requires paired storage, records
the first publication time, and removes pending keys. Low-level `mark_enumerated`
retains the first snapshot checkpoint and removes pending keys; the package helper
is mandatory to establish child durability before calling it. Lost acknowledgments
are resolved by retrying against durable facts. No S3/SNS verification or exactly-
once external publication is claimed by these database methods.

Failure updates compare revisions and chronological attempt timestamps. Reuse the
same aware timestamp/error when retrying one logical failure operation. Equal
repeats and older attempts are no-ops; equal timestamps with different errors
conflict. Newer attempts increment retry_count exactly once through CAS, with
error text truncated to 2048 characters. The counter is chronological diagnostics,
not an exact concurrent audit ledger. Completed work ignores late failure writes
and never regains pending eligibility. No terminal/DLQ state is introduced yet.

Current AWS guidance consulted through aws-mcp:

- [Conditional writes and idempotence](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/WorkingWithItems.html)
- [Sparse index membership](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/bp-indexes-general-sparse-indexes.html)
- [GSI eventual consistency](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/GSI.html)
- [Query pagination](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Query.Pagination.html)
- [Python SDK concurrency/configuration](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/programming-with-python.html)
- [BatchWriteItem lacks conditions](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/BestPractices_ConditionalBatchUpdate.html)

The SDK skill's example suggesting direct float serialization conflicts with the
[Boto3 serializer](https://docs.aws.amazon.com/boto3/latest/_modules/boto3/dynamodb/types.html),
which rejects floats. Adapters follow SDK behavior. Generic Streams/outbox and live
benchmark recommendations are outside this approved phase and architecture.

### 6.7 Phase 4 processing and dead-letter checkpoints

The four tables and GSI definitions in section 6.5 remain unchanged. Schema version
1 adds optional attributes, omitted on old Phase 3 rows and defaulted when read:

- `enumeration_failures`, `acquisition_failures`, `publication_failures`: nonnegative
  integer counts of failed workflow attempts, independent of transport/SDK retries.
- Paired `failure_stage`, `failure_error`, `failure_at`: latest chronological stage
  failure; each logical failure retry reuses the same aware timestamp/error.
- Paired `terminal_stage`, `terminal_error`, `terminal_error_type`, `terminal_at`:
  immutable final failure observation. Error/type text is bounded to 2048 characters.
- Optional `dead_lettered_at`: first successful operational send checkpoint.

FilingCheckpoint accepts only enumeration failures. ArtifactCheckpoint is separate
from Artifact and accepts only acquisition/publication failures. Publication
failures require stored source facts. Terminal stage must match unfinished work;
completed enumeration/published artifacts cannot be terminal. Validators reject
partial facts, invalid counters and inconsistent pending membership. Old Phase 3
records remain readable; older adapter code cannot process new terminal membership
and must not run alongside the Phase 4 writer.

`record_stage_failure(identity, stage, error, at, max_failures=..., error_type=...,
terminal=False)` updates the stage count, failure observation, applicable legacy
diagnostics and terminal decision in one revision-guarded write. Equal matching
observations and older observations do not count twice. CAS decisions reread
durable progress; late acquisition failures cannot terminalize publication and
late enumeration/publication failures cannot resurrect completed work. New stage
observations do not rewind newer legacy diagnostic timestamps. WorkControl uses
strictly increasing observation times when the clock repeats.

Terminal work atomically changes `pending_work_kind` to `DEAD_LETTER`, retaining
the original discovery sort. The existing PendingFilingEnumeration and
PendingArtifactWork indexes query this additional partition value. The public
filing `list_pending(kind="ENUMERATE" | "DEAD_LETTER", ...)` and artifact
`list_pending("ACQUIRE" | "PUBLISH" | "DEAD_LETTER", ...)` hydrate strong base
records and discard stale candidates as before. No new table/index, scan, TTL,
age filter, stream, transaction or transient queue state is added.

The worker creates `WorkFailure` from durable canonical source/terminal facts.
It emits `ingestion.work_failed` / `1.0` with failure_id, work_type/work_id, stage,
terminal time, error/type, stage failure count, CIK/ticker/form/accession, source
URL and optional S3 URI; it contains no bytes. Identity is
`<filing|artifact>/<source-identity>/<stage>/<terminal-time>` with no content hash.
JSON escapes unsafe SQS control characters and is bounded below default SNS/SQS
message limits. SQS SendMessage must return MessageId before
`mark_dead_lettered(identity, at)` records the first send time and removes pending
keys. Failed/lost acknowledgments remain recoverable; duplicate sends have the
same failure ID. The terminal source facts are never deleted or rewritten.

Terminal enumeration is durable even if no artifact exists. Partially created
children cannot acquire/publish before parent completion; a terminal parent keeps
them deferred, and its failure envelope covers the blocked package. A deferred
child can remain a recovery candidate, but pagination continues to other rows.
Normal checkpoint methods reject terminal redrive. Operator investigation uses
the durable checkpoint/envelope; a state-changing redrive contract is future work.

Failure recording uses finite checkpoint retries with the same original logical
failure timestamp. If persistence is unavailable, errors propagate and the source
remains recoverable; successful terminal handling is never fabricated. Retryable
SEC/incomplete-package, transient SDK and CAS errors consume configured stage
budgets. Invalid source metadata/storage conflicts become terminal immediately.
Shared permission/credential/resource/configuration errors propagate instead of
dead-lettering every company. Cancellation is never a failed workflow attempt.

Operational SQS is an ingestion-owned failed-work queue, separate from downstream
consumer queues and SNS subscription-delivery DLQs. Phase 4 only sends to configured
existing resources. Provisioning, retention, alarms and queue policies remain
Phase 8, with HTTPS/encryption and least-privilege access required for deployment.

AWS behavior verified through AWS MCP includes [conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html),
[HEAD permissions](https://docs.aws.amazon.com/AmazonS3/latest/API/API_HeadObject.html),
[metadata limits](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingMetadata.html),
[object key limits](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-keys.html),
[SNS Publish](https://docs.aws.amazon.com/sns/latest/api/API_Publish.html), and
[SQS SendMessage](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_SendMessage.html).
No live resource inspection/mutation was performed.

## 7. Calendar synchronization

### 7.1 Cadence

- Full upcoming-calendar sync: once daily.
- Optional near-term refresh: configurable; likely every few hours for the next 1–3 days if provider behavior warrants it.
- On service startup: load relevant near-term records from DynamoDB.

### 7.2 Update behavior

Provider changes should update the expected calendar record. This is planning data, not immutable regulatory data.

The ingestion service should log when an expected date/time materially changes.

## 8. Polling scheduler algorithm

Pseudo-flow:

```text
startup
  -> load enabled company universe
  -> load near-term calendar records
  -> build in-memory polling schedule

loop
  -> determine which companies are due
  -> enqueue due companies in asyncio.Queue
  -> compute next due time based on active/safety window
  -> sleep until next scheduler tick
```

The scheduler should not bypass the SEC rate limiter.

### 8.1 Polling cadence

Configuration should support:

```text
ACTIVE_POLL_INTERVAL_SECONDS=5..10
SAFETY_POLL_INTERVAL_SECONDS=<TBD, likely hours>
```

Do not hard-code market times throughout the codebase. Centralize window calculation in one policy component.

## 9. SEC request queue and rate limiter

### 9.1 In-memory queue

Use `asyncio.Queue` in v0.

Producers:

- polling scheduler;
- retry/recovery logic where applicable.

Consumers:

- small worker pool inside the same ECS task.

### 9.2 Rate limiter

All SEC requests acquire permission from one process-wide limiter.

Requirement:

```text
<= 5 outbound SEC requests per second across the entire task
```

A token-bucket or equivalent implementation is acceptable.

The limiter must apply to all SEC endpoints, including submissions, filing index, and document downloads. This means artifact downloads consume the same request budget as discovery polling.

### 9.3 Fairness

Avoid allowing one filing with many exhibits to indefinitely starve company polling. A simple fair queue or separate logical work classes may be introduced if replay tests show starvation. Do not overengineer this preemptively.

## 10. Discovery algorithm

For each due company:

1. call SEC company submissions endpoint;
2. parse recent filings;
3. filter to relevant forms;
4. compare accession numbers against the Filings repository;
5. for each unseen filing, atomically create the filing record;
6. fetch filing index/package metadata;
7. enumerate primary document + attached documents/exhibits;
8. create artifact records if absent;
9. enqueue/download artifacts.

Phase 3 clarification: an existing accession is not evidence that enumeration is
complete. The Phase 4 discovery service must inspect FilingCheckpoint, finish
pending enumeration through PackageCheckpoint, and only then dispatch acquisition.
PackageCheckpoint never enqueues, downloads, stores, or publishes content.

Relevant forms:

```text
8-K
10-Q
10-K
8-K/A
10-Q/A
10-K/A
```

The system should not assume that only EX-99.1 matters.

## 11. Artifact acquisition algorithm

For each artifact:

1. read artifact record;
2. if `stored_at` and `s3_uri` already exist, skip download;
3. download from SEC;
4. write bytes to deterministic S3 key;
5. update DynamoDB with `s3_uri`, `stored_at`, `content_type`, `size_bytes`;
6. if `published_at` is absent, publish `ArtifactReady` to SNS;
7. update `published_at` after successful publish.

If SNS publish fails after storage, do **not** download again. Retry publication only.

## 12. Restart/recovery algorithm

On startup:

1. reload calendar state and rebuild in-memory schedules;
2. find incomplete filings/artifacts and pending terminal sends through sparse indexes;
3. for records with no `stored_at`, retry acquisition;
4. for records with `stored_at` but no `published_at`, retry SNS publication;
5. completed records require no action.

Phase 3 implements the deliberate sparse-index access pattern in section 6.5.
Recovery queries are bounded by paginated candidate count, without a recent-age
cutoff. Old unfinished filings and artifacts remain indexed; avoid table scans.
Phase 4 provides RecoveryService.run_pass(), traversing enumeration, acquisition,
publication and dead-letter candidates to token None, including empty actionable
pages. Work errors are counted/logged without starving later candidates; shared
infrastructure/configuration failures propagate. Recheck parent/source facts before
acting. Continuous repeat-pass cadence remains Phase 6 runtime work.

## 13. Retry and failure behavior

### 13.1 Retryable conditions

Examples:

- network timeout;
- connection reset;
- SEC 429;
- SEC 5xx;
- temporary S3/DynamoDB/SNS failures.

### 13.2 Backoff

Use exponential backoff with jitter.

Exact values are configuration/TBD. Keep retry policy centralized.

### 13.3 Failure logging

Every failed attempt logs:

```text
timestamp
operation
cik
ticker
accession_number (if known)
artifact_id (if known)
attempt_number
error_type
error_message
will_retry
```

When a later retry succeeds, emit a success/recovery log that makes the sequence observable.

### 13.4 Dead-letter handling

Terminal work that exceeds retry limits should reach an operational dead-letter path. The implementation may use a dedicated SQS DLQ or another explicit failed-work queue depending on the final worker orchestration. CloudWatch alarm when dead-letter depth > 0.

## 14. Logging and metrics

### 14.1 Structured logs

Use JSON/structured logging suitable for CloudWatch querying.

Recommended common fields:

```text
service
operation
cik
ticker
accession_number
artifact_id
request_id/correlation_id
timestamp
```

### 14.2 Metrics

Emit custom metrics for:

- `SecRequests`;
- `SecThrottles`;
- `SecRequestErrors`;
- `FilingsDiscovered`;
- `ArtifactsStored`;
- `ArtifactPublishFailures`;
- `RetryAttempts`;
- `DiscoveryLatencyMs`;
- `DownloadLatencyMs`;
- `IngestionLatencyMs`;
- `CalendarSyncAgeSeconds`.

## 15. Configuration

Configuration should come from environment variables and/or AWS configuration sources, not hard-coded constants.

Minimum configuration:

```text
AWS_REGION
ARTIFACT_BUCKET
FILINGS_TABLE
ARTIFACTS_TABLE
CALENDAR_TABLE
COMPANIES_TABLE
ARTIFACT_READY_TOPIC_ARN
SEC_USER_AGENT
SEC_MAX_REQUESTS_PER_SECOND=5
ACTIVE_POLL_INTERVAL_SECONDS
SAFETY_POLL_INTERVAL_SECONDS
CALENDAR_LOOKAHEAD_DAYS
CALENDAR_PROVIDER
LOG_LEVEL
```

Provider API keys/secrets belong in AWS Secrets Manager or Parameter Store, not source control.

## 16. Docker/runtime

Container entry point:

```text
python -m finbot_ingestion.main
```

`main` should:

1. load configuration;
2. initialize AWS/provider/SEC clients;
3. run startup recovery;
4. start calendar refresh task;
5. start scheduler task;
6. start SEC worker(s);
7. handle graceful shutdown.

The service must handle SIGTERM from ECS and stop accepting new work while allowing reasonable in-flight cleanup.

## 17. CDK infrastructure

CDK should provision at minimum:

- ECR repository;
- ECS cluster/service/task definition;
- CloudWatch log group;
- S3 artifact bucket;
- DynamoDB tables;
- SNS ArtifactReady topic;
- IAM task role/policies;
- CloudWatch alarms;
- any DLQ used by the service.

Downstream consumer SQS queues may live in their own repositories/stacks because each consumer owns its queue, but the SNS topic is owned by ingestion.

## 18. GitHub Actions

On merge/push to `main`:

1. checkout repository;
2. configure Python;
3. install dependencies;
4. run lint/type checks if configured;
5. run pytest;
6. build Docker image;
7. authenticate to AWS using GitHub OIDC where practical;
8. push tagged image to ECR;
9. update ECS task/service to deploy the image;
10. fail workflow if tests/build/deployment fail.

CDK infrastructure deployment remains manual initially.

## 19. Test plan

### 19.1 Unit tests

Cover:

- earnings-window calculation;
- scheduler due-time logic;
- SEC form filtering;
- SEC response parsing;
- accession/artifact identity generation;
- S3 key generation;
- rate limiter behavior;
- retry/backoff policy;
- restart-state inference;
- event serialization/schema.

### 19.2 Integration tests

Mock SEC and AWS boundaries and test:

```text
new filing
 -> filing record
 -> filing index parsing
 -> artifact download
 -> S3 persistence
 -> DynamoDB update
 -> SNS publish
```

Also test:

- duplicate filing;
- duplicate artifact;
- SNS failure after successful S3 write;
- DynamoDB failure;
- S3 failure;
- SEC 429/5xx;
- restart with incomplete artifact.

### 19.3 Replay tests

Use historical filing metadata/HTML fixtures to simulate real earnings filing discovery and package enumeration.

### 19.4 Live smoke tests

Optional/manual only. Do not make routine CI depend on live SEC calls.

## 20. Codex implementation rules

Before changing architecture, Codex should read:

1. `docs/HLD.md`;
2. `docs/LLD.md`;
3. relevant ADRs.

Implementation rules:

- do not add AWS services without an architectural reason;
- keep SEC/provider-specific details behind adapters;
- preserve deterministic identities and idempotency;
- raw regulatory history is immutable;
- do not add Company Facts ingestion in v0;
- do not introduce global EDGAR ingestion in v0;
- do not introduce multiple ingestion tasks/distributed rate limiting in v0;
- add tests for failure/restart behavior, not just happy paths;
- update ADR/HLD/LLD if an architectural decision changes.
