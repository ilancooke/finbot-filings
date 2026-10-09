# finbot SEC ingestion

This package captures the current design for the SEC document discovery and download service that will act as the ingestion layer for the broader finbot platform.

The documents are intentionally scoped to **v0**. They preserve extension points for future scale and new downstream consumers, but avoid building distributed infrastructure before it is needed.

## Implementation status: Phase 5 (placeholder provider)

`finbot_ingestion` provides domain contracts, configuration validation, SEC URL
construction, submissions parsing, shared SEC transport, package enumeration,
and durable DynamoDB repositories/checkpoints with indexed recovery access.
Phase 4 adds conditional S3 storage, SNS artifact publication, an operational SQS
dead-letter adapter, discovery/acquisition workers, and explicit recovery passes.
Phase 5 adds explicit earnings-calendar synchronization, complete scoped snapshots,
guarded cancellation/date-move reconciliation, durable sync/freshness checkpoints,
and a placeholder provider. No live calendar provider has been selected.
It coexists with the existing
`finbot_filings` package and `finbot-filings` CLI, whose behavior is unchanged.
See [README-legacy.md](README-legacy.md) for those local workflows.

The distribution is still named `finbot-filings`; both Python namespaces are
installed from this repository. Boto3 requires version 1.43.110 or newer for the
tested conditional-write API contract.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m compileall src tests
.venv/bin/python -m pytest
```

The new code is organized as follows:

- `domain/`: `Company`, `ExpectedEarningsEvent`, `Filing`, `Artifact`,
  `ArtifactReady`, identity validation, and UTC timestamp helpers.
- `sec/submissions.py`: pure parsing of supplied SEC JSON, adapted from the
  legacy submissions parser, without requests, ticker lookup, or a count limit.
- `sec/urls.py`: official submissions, filing-index, and document URLs.
- `sec/client.py`, `sec/rate_limiter.py`: shared transport and request budgeting.
- `sec/filing_index.py`: pure package parsing and artifact enumeration.
- `ingestion/retry_policy.py`: centralized retry timing.
- `config.py`: `IngestionConfig`, reading process environment or an injected
  mapping. It does not load `.env` automatically or require legacy output paths.
- `repositories/`: async company/calendar/filing/artifact protocols, typed pages,
  persistence errors, and `PackageCheckpoint` for durable child creation.
- `repositories/dynamodb/`: low-level Boto3 adapters, serialization, isolated AWS
  configuration, and bounded SDK execution. No clients are created on import.
- `domain/checkpoints.py`: filing enumeration/failure facts separate from `Filing`.
- `calendar/`: normalized snapshot/provider contracts, isolated configuration,
  explicit refresh service and an unconfigured placeholder provider.
- `storage/`: conditional S3 creation and validated existing-object inspection.
- `messaging/`: SNS ArtifactReady and operational SQS terminal-failure envelopes.
- `ingestion/`: discovery, acquisition/publication, stage retries and recovery.
- `execution.py`, `aws_execution.py`, `aws_config.py`: bounded blocking I/O,
  reusable SDK clients, attempt logging and isolated storage/messaging settings.

For example, parse a local submissions fixture without contacting SEC:

```python
import json
from datetime import datetime, timezone
from pathlib import Path

from finbot_ingestion.domain import Company
from finbot_ingestion.sec.submissions import parse_company_submissions

payload = json.loads(Path("tests/fixtures/submissions_mixed.json").read_text())
filings = parse_company_submissions(
    Company(ticker="AAPL", cik="320193", name="Apple Inc."),
    payload,
    discovered_at=datetime(2026, 10, 8, tzinfo=timezone.utc),
)
```

The parser accepts all six supported forms and preserves amendments under their
own accessions. It returns unique filings newest-first by `filed_at`, with
accession as a deterministic tie-breaker. Identical repeated accessions collapse;
conflicting metadata for one accession raises `SECDataError`.

`filed_at` currently means the supplied SEC `acceptanceDateTime`, normalized to
UTC; it is not a measured public availability time. Missing, invalid, or
timezone-naive acceptance timestamps fail explicitly. `filingDate` and
`reportDate` never supply a fallback timestamp. `primaryDocument` may be absent,
null, or empty; the resulting name is `None` for later package enumeration.
An invalid relevant row rejects the parse; callers must not treat that response
as fully processed. Supplied parallel columns must have matching lengths.

Artifact IDs are `<dashed-accession>/<original-filename>`, computed by `Artifact`
and `domain.identity.artifact_identity()`. Filenames preserve case, must be
basenames, and cannot contain path separators or control characters. No content
hash is used. `artifact_key()` produces `<10-digit-cik>/<artifact-id>`.

`ArtifactReady.from_artifact(filing, artifact)` requires matching filing identity
and populated `s3_uri`/`stored_at`; `to_dict()` and `to_json()` serialize the
version `1.0` contract with UTC timestamps and no document contents. This validates
the record, not an actual storage commit; the Phase 4 worker enforces the
S3 → DynamoDB → SNS ordering. Artifact storage checkpoints require `s3_uri` and
`stored_at` together, and publication requires a storage checkpoint.

SEC configuration:

| Environment variable | Requirement |
| --- | --- |
| `SEC_USER_AGENT` | Required, nonempty identifying header; no fabricated fallback |
| `SEC_MAX_REQUESTS_PER_SECOND` | Defaults to `5`; must be finite, positive, and at most `5` |

Export these variables before calling `IngestionConfig.from_env()`, or pass a
mapping directly. `.env.example` retains legacy settings and documents the new
ceiling. The new SEC client enforces this ceiling through an explicitly shared limiter.
The legacy client's throttling remains unchanged.

## SEC transport and package discovery

```python
from finbot_ingestion.config import IngestionConfig
from finbot_ingestion.sec.client import SecClient
from finbot_ingestion.sec.rate_limiter import SECRateLimiter

config = IngestionConfig.from_env()
# Construct once and share across every SEC client/caller in the service.
limiter = SECRateLimiter(config.sec_max_requests_per_second)
with SecClient(config, limiter=limiter) as client:
    filings = client.get_company_submissions(company)
    for filing in filings:
        package = client.get_filing_index(filing)
        for document in package.documents:
            downloaded = client.download_document(document.sec_url)
            # downloaded.content: original response-content bytes; no text conversion.
```

This example contacts SEC when run; routine tests use fake transports only.
`company` is a domain `Company` with a curated CIK. Submissions returns typed
`Filing` records, package discovery returns `FilingIndex`, and downloads return
`DownloadedDocument` (`content`, `content_type`, `source_url`, `size_bytes`).
`package.artifacts(filing, discovered_at=...)` creates deterministic domain records
without marking them stored or published.

The synchronous `requests` client reuses a session and serializes HTTP attempts
under the shared limiter. Phase 4 services use a shared bounded executor.
There is one in-flight HTTP attempt at a time; slow responses can reduce throughput
below five requests/second. Retries and manually followed redirects use the same
budget. No successful JSON response is permanently cached.

Package discovery reconciles HTML document tables with accession-directory JSON.
It includes every directory file except recognized index/navigation files, retains
original names and available document types, and requires a resolvable primary
filing. Raw XML, images, and other auxiliary files are included without interpretation.
Missing/inconsistent metadata raises `SECIncompletePackageError`; unsafe or malformed
metadata raises `SECDataError`. The caller must retry the complete package snapshot
later, never checkpoint the error as success. A validated snapshot cannot guarantee
that SEC will not subsequently add another file. Durable child checkpoints and
unfinished-enumeration recovery are implemented; automatic rechecking of completed
snapshots remains a later policy.

Additional exported environment variables:

| Variable | Default |
| --- | --- |
| `SEC_CONNECT_TIMEOUT_SECONDS` | `10` |
| `SEC_READ_TIMEOUT_SECONDS` | `30` |
| `SEC_MAX_ATTEMPTS` | `3` |
| `SEC_BACKOFF_BASE_SECONDS` | `1` |
| `SEC_BACKOFF_CAP_SECONDS` | `30` |
| `SEC_MAX_REDIRECTS` | `5` |

Timeouts/backoff must be finite and positive; the cap must be at least the base.
Attempts must be a positive integer and redirects a nonnegative integer.
Timeouts/connection failures, HTTP 403/429/5xx, and package-index 404 responses
receive bounded retries. Other HTTP errors fail immediately. Backoff uses full
jitter; `Retry-After` seconds/dates influence delay up to the configured cap.
Redirects are restricted to official HTTPS SEC hosts and each hop consumes budget.
Failures and retry recovery use standard Python logging with structured extra fields.

Live calendar-provider integration, scheduling, continuous runtime, Docker/CDK,
and deployment remain future work. There is no
`finbot_ingestion.main` runtime yet. Legacy source and shared data remain intact.

## DynamoDB persistence and recovery

Phase 3 implements four separate tables; it does not provision them. See
[LLD section 6.5](docs/LLD.md#65-phase-3-concrete-schema-and-access-patterns) for the
table/index contract that future CDK must implement. Adapter operations assume
those tables and indexes already exist; missing resources fail explicitly.

Configure `DynamoDBConfig.from_env()` separately from SEC settings:

| Variable | Default / requirement |
| --- | --- |
| `AWS_REGION` | Required |
| `COMPANIES_TABLE` | Required |
| `CALENDAR_TABLE` | Required |
| `FILINGS_TABLE` | Required |
| `ARTIFACTS_TABLE` | Required; all four table names must differ |
| `DYNAMODB_CONNECT_TIMEOUT_SECONDS` | `5`, finite and positive |
| `DYNAMODB_READ_TIMEOUT_SECONDS` | `10`, finite and positive |
| `DYNAMODB_MAX_ATTEMPTS` | `3` total SDK attempts, standard retry mode |
| `DYNAMODB_PAGE_SIZE` | `100`, integer from 1 to 1000 |
| `DYNAMODB_MAX_WORKERS` | `4`, positive integer |
| `DYNAMODB_CAS_ATTEMPTS` | `4`, positive integer |
| `DYNAMODB_MAX_CALENDAR_RANGE_DAYS` | `366`, positive integer |

Parsing configuration never resolves credentials or contacts AWS. Existing SEC
configuration and the legacy CLI do not require any of these new settings.

The following wiring example contacts AWS **only when repository methods run**;
client construction explicitly opts into the normal AWS credential chain. It is
not a test or a provisioning command:

```python
from finbot_ingestion.repositories.dynamodb import (
    DynamoDBConfig, DynamoDBExecution, DynamoDBFilingRepository,
    DynamoDBArtifactRepository,
)
from finbot_ingestion.repositories.package_checkpoint import PackageCheckpoint

config = DynamoDBConfig.from_env()
# Share one execution/client across repositories and one application event loop.
with DynamoDBExecution.from_config(config) as execution:
    filings = DynamoDBFilingRepository(execution)
    artifacts = DynamoDBArtifactRepository(execution)
    checkpoint = PackageCheckpoint(filings, artifacts)
    # Inside the caller's async context:
    # await filings.create_if_absent(filing)
    # await checkpoint.persist(
    #     filing, package.artifacts(filing, discovered_at=discovered_at),
    #     primary_document_name=package.primary_document_name,
    #     completed_at=completed_at,
    # )
```

Repository calls offload blocking Boto3 I/O to a bounded executor. Close the shared
execution after awaited work finishes; it owns its default executor and closes the
client. Inject a client/executor for tests or future runtime wiring.

Filing/artifact creation uses conditional writes. Compatible duplicates return
`False` and preserve original observations, ticker, and checkpoints; incompatible
source identity/provenance raises `RepositoryConflict`. Artifact creation accepts
only newly discovered records. `PackageCheckpoint.persist()` verifies parent and
child identities and confirms every child with a strong base-table read before
committing completion. `mark_enumerated()` is the low-level commit primitive;
future discovery callers must use the helper rather than call it prematurely.

First enumeration completion timestamp, resolved primary name, and snapshot count
are retained. Replaying a later validated snapshot can add missing children through
the helper without changing those first-completion facts. Completion is an observed
snapshot, not proof that SEC will never add files; automatic recheck policy is later
orchestration. Existing accessions must not suppress unfinished enumeration.

`mark_stored()` records URI/time/content metadata together and transitions pending
work from `ACQUIRE` to `PUBLISH`. Compatible repeats preserve the first timestamp;
different URI/content metadata raises a conflict. `mark_published()` requires
storage, preserves the first publication timestamp, and removes pending keys.
These methods validate database checkpoints; they do not verify S3/SNS calls.
The Phase 4 worker remains responsible for external commit ordering.

`list_pending()`, `list_for_filing()`, and `list_enabled()` return `Page(items,
next_token)`. Tokens are opaque SDK continuations scoped to the region, table, index,
query, and page size. Continue until `next_token is None`, even if `items` is
empty. Pages are bounded by **candidate** count before base-record validation.
Recovery has no discovery-age cutoff or TTL and does not depend on a company
remaining enabled. GSIs are eventually consistent; stale candidates are checked
with strong base reads. Repeat complete passes later to catch delayed entries.
A single empty query cannot establish that all work is complete.

Failure recording uses strictly increasing, aware attempt timestamps. Retry one
logical failure-recording operation with the same timestamp/error. Equal repeats
and older failures do not increment the counter; an equal timestamp with a
different error conflicts. Error text is bounded to 2048 characters. Retry counts
are chronological operational diagnostics, not an exact concurrent audit ledger.
Failures after completion cannot resurrect pending work.

Calendar upserts are per-row and reject equal-sync-time conflicting observations;
older syncs cannot replace newer records or cancellation tombstones. Replacing a
different provider at an occupied key fails explicitly. `get_events(start, end)` selects the
inclusive UTC **dates** containing those aware datetimes, not intraday event times,
and queries/paginates each date, returning only active expectations. Phase 5
implements scoped cancellations and successful-sync tracking as described below.
Company upserts intentionally replace mutable
curated configuration, including enabled status.

Persistence schema version `1` uses fixed-width microsecond UTC timestamps,
omitted optional fields, validated integer counters, and a reversible JSON string
for raw calendar diagnostic payloads. Event schema `1.0` is unchanged. Oversized
items, overlong UTF-8 index keys, and malformed records fail explicitly; there are
no document bytes/hashes. Source identities are never truncated to fit indexes.
Normal tests block network access, inject dummy credentials, and use Stubber or
mocked SDK calls. No live SEC or AWS validation has been performed.

## Restart-safe acquisition and publication

Phase 4 exposes `DiscoveryService.discover(company)`,
`IngestionWorker.discover_company(company)`, `process_filing(accession_number)`,
`process_artifact(artifact_id)` and `RecoveryService.run_pass()`. These are explicit
async calls, not a scheduler or continuous daemon. Share one SEC client/limiter,
one SEC executor, one discovery/worker instance and its identity-lock registry.
The worker bounds entire in-flight artifact workflows, not only SDK calls.

The ordered path is **S3 → DynamoDB stored checkpoint → SNS → publication
checkpoint**. Before downloading an unstored record, the worker inspects S3.
Compatible existing objects repair database metadata without downloading or
replacing bytes. S3 writes always use `IfNoneMatch="*"`. Objects carry versioned
canonical source/discovery provenance; missing or conflicting metadata fails
explicitly. `stored_at` is S3's original `LastModified`, for both initial creation
and repair. The adapter preserves original filenames in keys and percent-encodes
special characters in S3 URIs; consumers decode the URI path to obtain the key.

Document downloads stream with `max_bytes` and stop on excess decoded bytes.
The worker default/maximum is 64 MiB per artifact, with two in-flight artifacts.
Acquisition temporarily retains a byte buffer and its immutable bytes copy; choose
container memory accordingly. Oversized artifacts become explicit terminal work.
The lower-level `download_document(url)` remains compatible; call
`download_document(url, max_bytes=...)` to use the bounded path. No application
content hashes or document interpretation are added.

Stored artifacts skip download on publication failure. Lost SNS acknowledgments
may republish the same schema `1.0` event with the same original metadata.
Delivery is **at least once**; consumers deduplicate by `artifact_id`.

Stage failure budgets survive restart and are separate for enumeration,
acquisition and publication. The legacy `retry_count` remains a chronological
diagnostic. Terminal observations switch the existing pending index to
`DEAD_LETTER` until an operational SQS send is checkpointed. Lost send
acknowledgments can duplicate an envelope with the same `failure_id`.
Partially created children wait for successful parent enumeration and remain
deferred if that parent becomes terminal. The parent supplies their operational
failure record. Successful terminal sends remove pending keys, while retaining
source/storage facts; normal workers never automatically redrive terminal work.

Recovery continues all candidate pages, including empty pages with tokens, and
does not stop at a failed or deferred row. Repeat full passes later to catch delayed
GSI entries. Submissions failures propagate without a success watermark so a later
company poll can rediscover work. Shared credential, permission and missing-resource
errors surface to the caller. Recovery summaries/logs make individual work errors
visible; a summary is not proof of global completion.

Additional isolated configuration (process environment or injected mappings):

| Variable | Default / requirement |
| --- | --- |
| `ARTIFACT_BUCKET` | Required general-purpose S3 bucket |
| `MAX_ARTIFACT_BYTES` | `67108864`; integer 1 through 67108864 |
| `ARTIFACT_READY_TOPIC_ARN` | Required standard SNS topic in `AWS_REGION` |
| `INGESTION_DEAD_LETTER_QUEUE_URL` | Required standard SQS queue in `AWS_REGION` |
| `INGESTION_AWS_CONNECT_TIMEOUT_SECONDS` / `INGESTION_AWS_READ_TIMEOUT_SECONDS` | `5` / `30`; finite, positive |
| `INGESTION_AWS_MAX_ATTEMPTS` / `INGESTION_AWS_MAX_WORKERS` | `3` / `2`; positive integers |
| `INGESTION_MAX_STAGE_FAILURES` | `3`; positive integer, per workflow stage |
| `INGESTION_CHECKPOINT_ATTEMPTS` / `INGESTION_DEAD_LETTER_ATTEMPTS` | `3` / `3`; positive integers |
| `INGESTION_MAX_INFLIGHT_ARTIFACTS` | `2`; positive integer |
| `INGESTION_RECOVERY_PAGE_SIZE` | `100`; integer 1 through 1000 |
| `INGESTION_BACKOFF_BASE_SECONDS` / `INGESTION_BACKOFF_CAP_SECONDS` | `1` / `30`; finite, positive; cap >= base |

SDK attempts and workflow attempts are both finite. SEC already retries each HTTP
request; a workflow attempt can contain those transport retries. These settings
do not become requirements for SEC configuration or legacy commands.

This wiring example opts into the AWS credential chain and contacts AWS/SEC when
run. It assumes the documented tables/indexes, bucket, topic and operational queue
already exist. No adapter provisions resources:

```python
import asyncio
from contextlib import ExitStack
from finbot_ingestion.aws_config import AWSIOConfig, StorageConfig, MessagingConfig
from finbot_ingestion.aws_execution import AWSExecution
from finbot_ingestion.execution import BlockingExecution
from finbot_ingestion.config import IngestionConfig
from finbot_ingestion.sec.client import SecClient
from finbot_ingestion.sec.rate_limiter import SECRateLimiter
from finbot_ingestion.repositories.dynamodb import (
    DynamoDBConfig, DynamoDBExecution, DynamoDBFilingRepository,
    DynamoDBArtifactRepository,
)
from finbot_ingestion.storage.s3_artifact_store import S3ArtifactStore
from finbot_ingestion.messaging.sns_publisher import SNSArtifactEventPublisher
from finbot_ingestion.messaging.dead_letter import SQSDeadLetterPublisher
from finbot_ingestion.ingestion.config import WorkflowConfig
from finbot_ingestion.ingestion.work_control import WorkControl
from finbot_ingestion.ingestion.discovery_service import DiscoveryService
from finbot_ingestion.ingestion.artifact_downloader import ArtifactDownloader
from finbot_ingestion.ingestion.ingestion_worker import IngestionWorker
from finbot_ingestion.ingestion.recovery_service import RecoveryService

sec_config = IngestionConfig.from_env()
aws_config, storage_config = AWSIOConfig.from_env(), StorageConfig.from_env()
messaging_config = MessagingConfig.from_env()
with ExitStack() as stack:
    db = stack.enter_context(DynamoDBExecution.from_config(DynamoDBConfig.from_env()))
    s3, sns, sqs = [stack.enter_context(AWSExecution.from_config(service, aws_config))
                    for service in ("s3", "sns", "sqs")]
    sec_execution = stack.enter_context(BlockingExecution(max_workers=1))
    limiter = SECRateLimiter(sec_config.sec_max_requests_per_second)
    sec = stack.enter_context(SecClient(sec_config, limiter=limiter))
    filings, artifacts = DynamoDBFilingRepository(db), DynamoDBArtifactRepository(db)
    control = WorkControl(WorkflowConfig.from_env())
    discovery = DiscoveryService(sec, sec_execution, filings, artifacts, control)
    downloader = ArtifactDownloader(sec, sec_execution, S3ArtifactStore(s3, storage_config),
                                    max_artifact_bytes=storage_config.max_artifact_bytes)
    worker = IngestionWorker(discovery, downloader,
        SNSArtifactEventPublisher(sns, messaging_config),
        SQSDeadLetterPublisher(sqs, messaging_config), filings, artifacts, control)
    summary = asyncio.run(RecoveryService(worker).run_pass())
```

Future runtime wiring must await all tasks before closing executors/clients.
Cancellation waits for blocking I/O to finish and leaves interrupted work recoverable.

Operational prerequisites for future deployment: HTTPS/encrypted bucket access,
`s3:GetObject`, conditional `s3:PutObject` and suitably scoped `s3:ListBucket` for
unambiguous absence checks; no delete/unconditional overwrite rights. A HEAD 403
is never treated as a missing object. DynamoDB repository permissions, SNS Publish
and operational SQS SendMessage must be limited to the configured resources.
The operational queue is distinct from consumer queues/subscription DLQs.
Storage inspection adds S3 requests and checkpoints add DynamoDB writes; no pricing
or throughput guarantee is inferred from the SEC ceiling. Policies, retention,
access logging, CloudTrail data events, CloudWatch metrics/alarms and queue setup
remain Phase 8 infrastructure work.

To investigate terminal work, inspect its durable checkpoint and the SQS
`ingestion.work_failed` envelope, then correct the underlying source/configuration
problem. Phase 4 intentionally exposes no automatic/manual mutation command to
redrive these records; an explicit operator redrive contract remains future work.
Do not delete raw S3 objects as a repair procedure.

## Earnings-calendar synchronization

Phase 5 exposes `CalendarSyncService.sync_once(start_date, end_date, kind="full")`,
`sync_full()`, `sync_near_term()` and `health()` as explicit async calls. Share one
service instance in one event loop; it serializes refreshes. There is no periodic
task/daemon or calendar CLI yet. Phase 6 will use the daily and optional near-term
cadence settings to schedule calls.

The default `PlaceholderCalendarProvider` always raises
`CalendarProviderNotConfigured`. It performs no HTTP calls, returns no invented
events, cannot clear expectations, and never establishes a successful-sync
checkpoint. Provider selection and a live adapter are deliberately deferred at
the user's request; the service and persistence can be used with injected adapters.

For repositories already constructed by the caller:

```python
from finbot_ingestion.calendar.service import CalendarSyncService
from finbot_ingestion.calendar.provider import CalendarProviderNotConfigured

service = CalendarSyncService.with_placeholder(company_repo, calendar_repo)
# Inside an async caller; repository methods contact AWS if using real adapters:
try:
    await service.sync_full()
except CalendarProviderNotConfigured:
    pass  # Expected until an actual provider is selected and injected.
health = await service.health()  # stale=True until a full sync succeeds.
```

An injected provider implements `EarningsCalendarProvider.fetch_events()` and
returns `CalendarSnapshot(provider, start_date, end_date, company_ciks, events,
complete)` (keyword-only fields). It must fully fetch/normalize the exact requested
inclusive date/company scope, including all pages, before claiming `complete=True`.
A failed page, unknown coverage, or a provider error must raise or return an
incomplete snapshot, never a successful empty calendar. Only a validated complete
empty snapshot can cancel in-scope expectations. Adapters map curated tickers to
CIKs explicitly and filter global provider results before returning the snapshot.
The service rejects out-of-scope events, ambiguous curated tickers, conflicting
duplicates and a stable provider event ID appearing on multiple dates. Distinct
quarters remain separate events. Provider secrets must never enter diagnostic
payloads; service failure logs/checkpoints record exception types, not arbitrary
provider messages or URLs.

The service assigns canonical curated tickers and a durable monotonic observation
time, normalizes missing earnings times to `unknown`, validates the whole snapshot,
and writes expectations before cancelling obsolete rows. Cancellation is limited
to the confirmed range, enabled-company snapshot and same provider. Disabled,
other-provider and out-of-range records remain untouched. A move across an
unconfirmed boundary can temporarily retain the old expectation until a refresh
covers it. Optional stable provider IDs identify date-change logs; no period
identity is guessed when IDs are unavailable.

Calendar keys remain `expected_date` / `cik`. Cancelled rows retain their metadata,
set `calendar_active=False` and advance `synced_at`; existing rows without this flag
are active. Tombstones prevent late retries from recreating removed expectations.
There is no TTL/deletion/cleanup policy in this phase. Do not run older calendar
readers alongside the Phase 5 writer: they do not understand inactive records.

Sync metadata uses reserved partition `expected_date="__calendar_sync__"` and
sort key `cik=<provider>` with a separately validated typed record. It holds the
latest run, revision, exact company/date scope, separate full/near-term successes
and sanitized failure facts. It is never returned by ISO-date queries. No fifth
table, index, scan, stream, hash or infrastructure change is required. Cloud IAM
key restrictions must allow this reserved partition in future deployment.

Success is recorded only after every expectation/cancellation write completes.
Lost acknowledgments retry the same request/timestamp/facts. Interrupted writes
may leave partial progress visible; the next complete refresh repairs it. No
batch-wide atomicity or durable snapshot replay during a provider outage is
claimed. Previously stored expectations and the last success remain available
after failed/incomplete provider fetches. `health()` reports age of the last full
success; near-term success cannot conceal stale full coverage. It reports recorded
scope, which callers must compare with a changed production universe/range.

Calendar settings are independent of SEC/AWS/legacy settings, with no implicit
`.env` loading or client creation:

| Variable | Default |
| --- | --- |
| `CALENDAR_PROVIDER` | `placeholder` (injected adapter name must match) |
| `CALENDAR_LOOKAHEAD_DAYS` / `CALENDAR_NEAR_TERM_DAYS` | `90` / `3`, inclusive day counts |
| `CALENDAR_FULL_REFRESH_SECONDS` | `86400` |
| `CALENDAR_NEAR_TERM_REFRESH_SECONDS` | `0` (automatic near-term refresh disabled) |
| `CALENDAR_STALE_AFTER_SECONDS` | `172800` |
| `CALENDAR_MAX_COMPANIES` / `CALENDAR_MAX_SNAPSHOT_EVENTS` | `1000` / `5000` |
| `CALENDAR_COMPANY_PAGE_SIZE` | `100`, integer 1 through 1000 |
| `CALENDAR_PROVIDER_ATTEMPTS` / `CALENDAR_CHECKPOINT_ATTEMPTS` | `3` / `3` |
| `CALENDAR_BACKOFF_BASE_SECONDS` / `CALENDAR_BACKOFF_CAP_SECONDS` | `1` / `30` |

Positive finite values are required except the optional zero near-term cadence;
near-term days cannot exceed lookahead. The requested date span must also fit
`DYNAMODB_MAX_CALENDAR_RANGE_DAYS`. Company/event counts are bounded. Retryable
provider/incomplete-snapshot and transient repository failures use the shared
jittered timing policy; permanent data/configuration/permission errors propagate.
The selected live adapter will own its HTTP timeouts, request-size bounds,
provider budget and credentials. Provider calls do not consume the SEC budget.

## Migration status

Phases 1–4 and the authorized Phase 5 placeholder scope are complete. See
[the migration plan](docs/MIGRATION_PLAN.md) for acceptance criteria and validation.
Next: Phase 6 — scheduling and continuous runtime. The live calendar adapter and
production universe remain unresolved inputs. The legacy CLI remains available.

## Documents

- [`docs/HLD.md`](docs/HLD.md) — high-level architecture, responsibilities, assumptions, data flow, AWS services, and scaling boundaries.
- [`docs/LLD.md`](docs/LLD.md) — implementation-oriented design for Codex: package structure, interfaces, data models, persistence, event schema, algorithms, retries, restart behavior, configuration, and tests.
- [`docs/adr/`](docs/adr/) — architecture decision records explaining the major design choices and when to revisit them.

## v0 summary

- Coverage: curated universe of approximately 500 companies.
- Earnings calendar: one free provider behind a swappable interface; provider selection is TBD.
- Discovery: calendar-driven, per-company SEC/EDGAR polling.
- Active polling: approximately every 5–10 seconds during earnings windows.
- SEC request ceiling: centralized client-side limit of 5 requests/second for the service.
- Relevant forms: 8-K, 10-Q, 10-K, plus amendments; download the primary filing document and all exhibits.
- Storage: immutable raw documents in S3; filing/artifact metadata and durable checkpoints in DynamoDB.
- Eventing: publish `ArtifactReady` to SNS after durable storage; downstream consumers receive through their own SQS queues.
- Runtime: one ECS/Fargate task for v0, with an in-memory scheduler/request queue.
- Observability: CloudWatch logs, metrics, and alarms; DLQ for exhausted retries.
- CI/CD: GitHub Actions runs tests, builds/pushes the Docker image to ECR, and updates ECS; CDK deploys infrastructure.
- Historical correctness: amendments/restatements create new immutable records so downstream clients can reproduce the information set available at any point in time.

## Explicitly deferred

- Global EDGAR latest-filings ingestion.
- SEC Company Facts ingestion.
- Multi-instance/distributed SEC rate limiting.
- Multiple earnings-calendar providers and reconciliation.
- RAG/indexing and earnings extraction logic; these are downstream consumers of this service.
