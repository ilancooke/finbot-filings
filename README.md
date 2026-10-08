# finbot SEC ingestion

This package captures the current design for the SEC document discovery and download service that will act as the ingestion layer for the broader finbot platform.

The documents are intentionally scoped to **v0**. They preserve extension points for future scale and new downstream consumers, but avoid building distributed infrastructure before it is needed.

## Implementation status: Phase 3

`finbot_ingestion` provides domain contracts, configuration validation, SEC URL
construction, submissions parsing, shared SEC transport, package enumeration,
and durable DynamoDB repositories/checkpoints with indexed recovery access.
It coexists with the existing
`finbot_filings` package and `finbot-filings` CLI, whose behavior is unchanged.
See [README-legacy.md](README-legacy.md) for those local workflows.

The distribution is still named `finbot-filings`; both Python namespaces are
installed from this repository. Phase 3 adds Boto3 for the AWS persistence adapters.

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
the record, not an actual storage commit; the future worker must enforce the
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
under the shared limiter. Later async runtime callers must use a bounded executor.
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
that SEC will not subsequently add another file. Durable reconciliation is Phase 3+.

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

S3 storage/SNS publication, recovery orchestration, calendar synchronization, scheduling,
continuous runtime, Docker/CDK, and deployment remain later phases. There is no
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
older syncs cannot replace newer records. `get_events(start, end)` selects the
inclusive UTC **dates** containing those aware datetimes, not intraday event times,
and queries/paginates each date. Date moves/cancellations and provider successful-
sync tracking remain Phase 5. Company upserts intentionally replace mutable
curated configuration, including enabled status.

Persistence schema version `1` uses fixed-width microsecond UTC timestamps,
omitted optional fields, validated integer counters, and a reversible JSON string
for raw calendar diagnostic payloads. Event schema `1.0` is unchanged. Oversized
items, overlong UTF-8 index keys, and malformed records fail explicitly; there are
no document bytes/hashes. Source identities are never truncated to fit indexes.
Normal tests block network access, inject dummy credentials, and use Stubber or
mocked SDK calls. No live SEC or AWS validation has been performed.

## Migration status

Phases 1–3 are complete. See [the migration plan](docs/MIGRATION_PLAN.md) for
acceptance criteria and validation. Next: Phase 4 — restart-safe S3/SNS acquisition
and publication. It has not started.

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
