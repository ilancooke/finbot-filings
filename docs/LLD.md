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
    async def put(
        self,
        *,
        cik: str,
        accession_number: str,
        filename: str,
        content: bytes,
        content_type: str | None,
    ) -> str: ...  # returns s3_uri
```

S3 key format:

```text
<cik>/<accession_number>/<filename>
```

Writes should be safe to retry against the deterministic key.

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

Potential future GSIs can support accession-number or company/time queries, but only add them when a concrete access pattern exists.

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
2. find recent/incomplete artifact records needed for recovery;
3. for records with no `stored_at`, retry acquisition;
4. for records with `stored_at` but no `published_at`, retry SNS publication;
5. completed records require no action.

Recovery queries should be bounded to a practical recent time window or supported through a deliberate DynamoDB access pattern; avoid unbounded table scans.

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
