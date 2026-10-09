# finbot SEC ingestion

`finbot-filings` is a focused SEC document-acquisition service. Its installed
Python namespace is `finbot_ingestion`. It discovers relevant filings, acquires
original document bytes, stores immutable artifacts in S3, maintains DynamoDB
metadata/checkpoints and publishes `ArtifactReady` events to SNS. Document
interpretation, earnings extraction, features and consumer queues belong downstream.

Phase 7 provides the supported runtime/container and removes the superseded
`finbot_filings` namespace, local bundle/extraction commands and legacy dependencies.
CDK, cloud deployment and application delivery remain Phase 8. No live earnings
calendar adapter or authoritative production company universe has been selected.
The placeholder reports unavailable calendar coverage; it cannot supply a calendar.

## Install and validate

Python 3.12 or newer is required. From this repository:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m pytest -q
.venv/bin/python -m build
```

Routine tests block network access and use temporary data, fake transports and
SDK Stubber/stateful clients. Docker lifecycle tests are separately enabled below.
`build` creates an sdist and a wheel under ignored `dist/`. Runtime dependencies
are Boto3, requests, BeautifulSoup and exchange-calendars. The latter needs pandas,
NumPy and timezone/calendar helpers; ingestion does not depend on PyArrow or lxml.

## Supported commands

```bash
.venv/bin/python -m finbot_ingestion.main --help
.venv/bin/python -m finbot_ingestion.main
# Separate process; checks only the local heartbeat:
.venv/bin/python -m finbot_ingestion.main --health-check
```

Normal execution contacts AWS and SEC. It requires an identifying SEC User-Agent,
existing AWS resources/indexes, valid AWS credentials and a nonempty enabled
company universe. It never provisions or seeds resources. Help needs no service
configuration. Health checks need only runtime settings and make no AWS/SEC calls;
exit status is zero for a fresh live heartbeat and one otherwise.

Export settings into the process environment. `.env.example` documents all names
and defaults; the application does not load `.env` files. `LOG_LEVEL` defaults to
`INFO`. AWS uses the normal SDK credential chain. Do not place credentials in source,
images or diagnostic payloads. The existing ignored local `.env` is left untouched;
its former legacy configuration is not sufficient for this runtime.

## Container workflow

Build again after application, dependency or image-configuration changes:

```bash
docker build -t finbot-ingestion:phase7 .
docker run --rm --network none finbot-ingestion:phase7 --help
docker run --rm --network none finbot-ingestion:phase7 --health-check
```

The final command returns one because that new container has no runtime heartbeat.
The image installs a wheel in Python 3.12 slim Linux, runs as UID/GID 10001, and
starts `python -m finbot_ingestion.main` directly as PID 1. It contains runtime
dependencies and timezone data; tests, build sources, credentials and shared data
are excluded. It exposes no inbound port. `/tmp` holds the atomic heartbeat.

For a configured local runtime, prepare a private Docker environment file:

```bash
cp .env.example .env.ingestion
# Edit example identity/resource values and supply valid runtime credentials.
# This starts real AWS/SEC work; run only with the intended existing resources.
docker run --rm --name finbot-ingestion \
  --env-file .env.ingestion --stop-timeout 120 \
  --read-only --tmpfs /tmp:rw,nosuid,size=32m \
  --cap-drop ALL --security-opt no-new-privileges \
  finbot-ingestion:phase7
```

The example file is in Docker `--env-file` format. Passing it is explicit; the
application still reads process variables only. For local Docker, credentials must
be supplied explicitly through your chosen SDK credential mechanism. AWS task-role
credentials will be configured in Phase 8. Never bake credentials into the image.
No `FINBOT_DATA_ROOT` mount is used by this cloud ingestion service.

From another terminal:

```bash
docker exec finbot-ingestion python -m finbot_ingestion.main --health-check
docker stop --timeout 120 finbot-ingestion
```

SIGTERM/SIGINT stops poll admissions and producers, drains bounded stage queues
within the configured grace, then cancels remaining tasks cooperatively. Blocking
HTTP/SDK calls finish before clients close. The default 30-second drain grace is
not a hard bound on total cleanup; Docker's stop timeout is separate. Forced
termination leaves interrupted work recoverable from durable checkpoints.

The image health check uses the local heartbeat every 30 seconds, with a five-second
command timeout, 120-second startup allowance and three retries. It checks liveness,
not full calendar readiness. Placeholder/stale calendar state remains visible in
health output and metrics. ECS health configuration and alarms remain Phase 8 work.

## Offline container validation

After rebuilding the image, run:

```bash
FINBOT_CONTAINER_TESTS=1 FINBOT_CONTAINER_IMAGE=finbot-ingestion:phase7 \
  .venv/bin/python -m pytest -q tests/integration/test_phase7.py -m container
```

Docker must be accessible from the execution environment. Opted-in tests fail if
Docker/the image is unavailable; ordinary pytest clearly skips these six cases.
They mount a test-only startup shim and reusable stateful boundaries outside the
image, leaving the production entry point, configuration, real SEC limiter,
repositories and supervision intact. Containers have `--network none`, dummy
credentials, read-only filesystems and temporary fixture state. Tests cover
installed imports/dependencies, health, PID 1, SIGTERM/SIGINT, original bytes and
publication, blocked I/O cleanup and unexpected required-loop failure/return.
No mock-mode switch is shipped in the application.

## Acquisition and durable contracts

Coverage is a curated universe of approximately 500 ticker/CIK/name/enabled records.
Ticker is convenience metadata; CIK normalizes to ten digits. Relevant forms are
`8-K`, `10-Q`, `10-K` and their `/A` amendments. Accessions retain dashed identity;
SEC archive URLs use numeric CIK and dash-free accession path components.

`filed_at` is SEC `acceptanceDateTime`, normalized to UTC, rather than a measured
public-availability instant. Missing, invalid or naive acceptance timestamps fail;
filing/report dates never substitute. Parsing returns all supported recent rows
without a count cutoff. Identical accession duplicates collapse; conflicts and
invalid relevant rows reject the response. A missing primary name can be resolved
by later package enumeration. Historical submissions backfill is not implemented.

Package discovery reconciles the filing's HTML document table and directory JSON.
It acquires the primary document and all observed documents/exhibits, including
PDFs, XML, images and auxiliary files, excluding recognized navigation/index files.
Missing/inconsistent snapshots are retried rather than checkpointed as complete.
Enumeration completion describes one observed snapshot; automatic completed-package
rechecking remains deferred.

Artifact IDs are `<dashed-accession>/<original-filename>`; filenames preserve case
and must be safe basenames. S3 keys are `<10-digit-cik>/<artifact-id>`. S3 URI paths
are percent-encoded; consumers decode the URI path to obtain the original key.
No application content hashes or interpretation are used. Amendments create new
immutable records and objects, preserving historical information sets.

The ordered path is **S3 → DynamoDB stored checkpoint → SNS → published checkpoint**.
S3 writes use `IfNoneMatch="*"`. Before downloading an unstored record, acquisition
inspects existing objects and validates canonical versioned provenance. Compatible
objects repair metadata without replacing bytes; missing/conflicting metadata
fails explicitly. `stored_at` is the original S3 HEAD `LastModified`, including repair.
HEAD permission errors never mean absence. Original discovery/ticker facts remain
canonical on duplicate discovery.

Downloads stream original response-content bytes with a maximum of 64 MiB per
artifact, configurable downward; oversized work fails explicitly. Two whole artifact
workflows run concurrently by default. Buffer conversion can retain two copies
per workflow temporarily; container memory must account for that. Stored artifacts
skip download when publication is retried.

`ArtifactReady` is `artifact.ready` / schema `1.0`, containing artifact/filing/CIK/
ticker/form/document/filename/S3 references and acceptance/discovery/storage times,
without document contents. Delivery is at least once. Consumers deduplicate by
`artifact_id` and own their subscribed SQS queues. Lost publication acknowledgments
can produce identical logical events again.

## Persistence, recovery and terminal work

Four existing tables and fixed indexes are required; all keys are strings:

| Table | Base partition / sort key | Required GSIs |
| --- | --- | --- |
| Companies | `cik` / none | `EnabledCompanies`: `enabled_marker` / `cik` |
| Calendar | `expected_date` / `cik` | None |
| Filings | `accession_number` / none | `PendingFilingEnumeration`: `pending_work_kind` / `pending_work_sort` |
| Artifacts | `artifact_id` / none | `PendingArtifactWork`: `pending_work_kind` / `pending_work_sort`; `ArtifactsByAccession`: `accession_number` / `filename` |

GSIs use KEYS_ONLY projection. See [LLD sections 6–8](docs/LLD.md) for exact schema,
revision guards, scoped pagination, sync/satisfaction records and compatibility.
The Calendar table must permit reserved `__calendar_sync__` and
`__event_satisfaction__` partitions. Inactive cancellation tombstones prevent stale
refreshes from restoring cancelled events. Do not run superseded adapter versions
against newer processing/calendar records.

Conditional creates and guarded updates preserve first observations/checkpoints.
Enumeration completes only after every child is confirmed durable through a strong
base-table read. Transient queue/download states remain in memory. Pending sparse
indexes drive repeated recovery without scans, age cutoffs or TTL, including work
from disabled companies. GSIs are eventual; candidates are strongly rechecked.
Continue every page, including empty pages with a token, and repeat full passes.
One empty pass never proves global completion.

Enumeration/acquisition/publication have separate persistent failure budgets.
Terminal facts precede operational SQS dead-letter sends, whose first successful
checkpoint removes pending eligibility. Lost sends can duplicate the stable
`failure_id`. Partially created children wait for completed parent enumeration;
a terminal parent keeps them deferred. Shared configuration/permission/resource
errors propagate instead of terminalizing every company.

Investigate `ingestion.work_failed` envelopes together with durable checkpoints.
Normal workers never automatically redrive terminal work. An operator mutation
command remains deferred; deleting raw objects is not a repair procedure.

## Calendar, scheduling and observability

Only `CALENDAR_PROVIDER=placeholder` is wired. It raises
`CalendarProviderNotConfigured`, never invents an empty snapshot and cannot clear
expectations or establish freshness. Injected provider contracts require complete,
validated snapshots of exact company/date scope before cancellation. Existing
expectations survive failed/incomplete refreshes. Full and near-term success scopes
are distinct; near-term refresh never establishes full freshness.

The runtime uses coarse five-minute durable reloads and memory-only scheduler ticks.
Completion-based active polling defaults to ten seconds; all-day safety polling to
one hour with CIK staggering. XNYS sessions handle DST, holidays and early closes.
Before-market windows span open minus two hours to open plus two hours;
after-market windows span close minus two hours to close plus three hours. Unknown
times span the full combined window; non-session expectations retain their date with
a 07:30–19:00 market-local fallback. Unsatisfied events get two hours of grace.

All SEC polls/indexes/downloads/retries/redirects share a five-request/second ceiling.
One HTTP attempt is in flight at a time through reused synchronous transport and a
bounded executor. Slow responses and competing work reduce throughput. Target poll
cadence and downstream latency are not unconditional guarantees. See the synthetic
[Phase 6 capacity replay](docs/PHASE_6_REPLAY.md).

Version `earnings-satisfaction-v1` stops aggressive polling only after a durable
satisfaction checkpoint for the same CIK and acceptance within window/grace. Exact
original `10-Q` and `10-K` qualify; original `8-K` also requires unambiguous SEC
Item `2.02` metadata. Amendments/generic or ambiguous 8-Ks remain ingested without
satisfying the event. Safety polling and pending recovery continue. This is a
scheduling heuristic, not evidence of extracted or validated earnings.

Required-loop failure, unexpected return or stalled busy work exits nonzero.
Sanitized JSON logs go to stderr; bounded CloudWatch EMF records go to stdout with
Service/Environment dimensions. Local health separates liveness from calendar
age/scope/provider configuration. CloudWatch collection, DLQ-depth monitoring and
alarms require Phase 8 infrastructure.

## Configuration reference

`.env.example` contains every supported setting. Configuration parsing does not
create clients, resolve credentials or load files implicitly. Region/table/storage/
messaging requirements apply to normal runtime execution; isolated domain/parser
utilities need only their own relevant settings.

| Group | Required settings / defaults |
| --- | --- |
| SEC | Required `SEC_USER_AGENT`; `SEC_MAX_REQUESTS_PER_SECOND=5` (finite, positive, at most 5) |
| SEC transport | `SEC_CONNECT_TIMEOUT_SECONDS=10`, `SEC_READ_TIMEOUT_SECONDS=30`, `SEC_MAX_ATTEMPTS=3`, `SEC_BACKOFF_BASE_SECONDS=1`, `SEC_BACKOFF_CAP_SECONDS=30`, `SEC_MAX_REDIRECTS=5` |
| AWS/DynamoDB | Required `AWS_REGION`, four distinct `COMPANIES_TABLE`, `CALENDAR_TABLE`, `FILINGS_TABLE`, `ARTIFACTS_TABLE` names |
| DynamoDB execution | Connect/read timeouts 5/10 seconds, 3 attempts, page size 100, 4 workers, 4 CAS attempts, maximum date span 366 days; `DYNAMODB_*` names in example |
| Storage/messaging | Required `ARTIFACT_BUCKET`, same-region standard `ARTIFACT_READY_TOPIC_ARN`, standard `INGESTION_DEAD_LETTER_QUEUE_URL`; `MAX_ARTIFACT_BYTES=67108864` |
| Ingestion | AWS connect/read 5/30 seconds, attempts/workers 3/2, stage failures 3, checkpoint/dead-letter attempts 3/3, inflight artifacts 2, recovery page 100, backoff 1/30 seconds; `INGESTION_*` names in example |
| Calendar | Placeholder provider, lookahead/near-term 90/3 days, full/near-term refresh 86400/0 seconds, stale after 172800 seconds, maximum companies/events 1000/5000, company page 100, provider/checkpoint attempts 3/3, backoff 1/30 seconds |
| Runtime | XNYS / America/New_York, active/safety 10/3600 seconds, reload/recovery/tick 300/60/1 seconds, refresh retry 60 seconds, poll/enumeration workers 2/1, company/filing/artifact queues 1000/100/200 |
| Runtime health | Metrics/heartbeat 5/30 seconds, stall allowance 1800 seconds, shutdown grace 30 seconds, `RUNTIME_HEALTH_PATH=/tmp/finbot-ingestion-health.json` |

Durations must be finite and positive except zero disables near-term refresh.
Backoff cap must be at least base; active cadence cannot exceed safety cadence.
Runtime worker counts are bounded 1–16 and queues 1–10,000. Health path must be
absolute; derived reload coverage must fit the repository date-span bound.
SEC retries include network/timeouts, 403/429/5xx and package-index 404, with bounded
jitter and Retry-After. Redirects stay on official HTTPS SEC hosts and consume budget.

Deployment must supply encrypted/HTTPS storage and least-privilege permissions:
conditional S3 PutObject, GetObject and scoped ListBucket for absence detection,
DynamoDB table/index access, SNS Publish and operational SQS SendMessage. Ingestion
must not delete or unconditionally replace raw objects. Resources/policies/retention
are Phase 8 work; no adapter provisions them.

## Design documents and legacy recovery

[HLD](docs/HLD.md), [LLD](docs/LLD.md), [ADRs](docs/adr/) and
[MIGRATION_PLAN](docs/MIGRATION_PLAN.md) remain the sources of truth.
[PHASE_7_PLAN](docs/PHASE_7_PLAN.md) records the cutover and validation. Prior phase
plans/results are historical records; use this README for current commands.

All removed legacy source, extraction/parsing tests, fixtures, docs, scripts and
sample tickers are recoverable at commit
`59cacd78a1da01d00b913c2e67185b0a0980d7ce`. Recover them without replacing current
source or touching shared data, for example from this repository:

```bash
git worktree add --detach ../finbot-filings-legacy 59cacd78a1da01d00b913c2e67185b0a0980d7ce
```

No permanent legacy subtree or compatibility CLI remains. Downstream relocation
requires separate work. Global EDGAR feeds, Company Facts, multiple calendar
providers, distributed rate limiting, multiple ingestion tasks, RAG and extraction
remain outside v0. Next milestone: Phase 8, CDK and application delivery.
