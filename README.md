# finbot SEC ingestion

`finbot-filings` is a focused SEC document-acquisition service. Its installed
Python namespace is `finbot_ingestion`. It discovers relevant filings, acquires
original document bytes, stores immutable artifacts in S3, maintains DynamoDB
metadata/checkpoints and publishes `ArtifactReady` events to SNS. Document
interpretation, earnings extraction, features and consumer queues belong downstream.

The supported runtime/container uses only `finbot_ingestion`; the superseded
namespace and extraction commands were removed in Phase 7. Phase 8 adds CDK,
application delivery, health alarms and the agreed conservative recovery policy.
Infrastructure and workflows are validated offline. The bootstrap, state and
runtime stacks are deployed. GitHub activation succeeded on 2026-10-10; one healthy
ARM64 task is running with fresh calendar coverage and verified SEC acquisition,
S3 storage and SNS publication checkpoints. See [DEPLOYMENT](docs/DEPLOYMENT.md)
for recorded results and the initial oversized-document failure requiring review.
The Yahoo/yfinance earnings-calendar adapter is implemented and tested offline.
Provider selection remains explicit; the default placeholder reports unavailable
coverage. The [initial production universe](docs/PRODUCTION_UNIVERSE.md) contains
50 user-selected symbols. The operator applied the standard CLI seed input;
read-only verification confirmed all 50 stored records match the reviewed SEC
ticker/CIK/name mapping. The operator's enabled-index query also returned all 50
companies. A complete local 30-day Yahoo collection passed with 49 observed
companies; the operator confirmed NVDA's absence was expected. The first full cloud
refresh completed for October 10–November 8 with 47 observations across the same
50-company scope. One Citigroup submission exceeded the configured 64-MiB artifact
limit and was recorded as terminal failed work; ingestion continues.

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
include Boto3, requests, BeautifulSoup, exchange-calendars, `yfinance==1.7.0`
and `curl_cffi==0.16.3`. The calendar libraries need pandas, NumPy, lxml and timezone
helpers. lxml is a yfinance dependency; no document extraction was reintroduced.
Ingestion does not depend on PyArrow.

## Supported commands

```bash
.venv/bin/python -m finbot_ingestion.main --help
.venv/bin/python -m finbot_ingestion.main
# Separate process; checks only the local heartbeat:
.venv/bin/python -m finbot_ingestion.main --health-check
# Separately launched Fargate task only; finite AWS check with isolated test writes:
.venv/bin/python -m finbot_ingestion.main --deployment-check --check-id readiness-20261009-1
# One-shot live Yahoo collection check; no AWS calls, SEC calls or runtime startup:
.venv/bin/python -m finbot_ingestion.calendar.check \
  --companies-file infra/seed/companies.prod.json \
  --days 30 \
  --output /private/tmp/finbot-calendar-check-20261009.json
```

The calendar check reads the reviewed seed file as company input; it never applies
the write requests. It uses the production Yahoo adapter, normal bounded request
settings and a new private temporary cache, then closes the provider and worker.
It fetches 30 inclusive calendar dates starting on today's America/New_York date.
Optional `--start-date YYYY-MM-DD` selects a reproducible starting date. Help is
offline; running the check makes live Yahoo requests. `YAHOO_*` environment
overrides apply to request bounds; no AWS credentials or SEC identity are needed.
The output path must be new so prior evidence is preserved. Exit zero means
complete, consistent collection for the requested scope; the report explicitly
lists companies without observations. It does not prove Yahoo supplied every
company's earnings date or approve activation. Raw payloads, cookies and provider
exception messages are excluded. See [DEPLOYMENT](docs/DEPLOYMENT.md) for review.

The deployment check refuses credentials outside the ECS task role. It checks
all 50 enabled company records, private writable `/tmp`, atomic heartbeat files,
bounded memory allocation, DynamoDB conditional test checkpoints and immutable
SSE-S3 test bytes. Its process budget is 180 seconds. It emits a sanitized report
and separate `Finbot/DeploymentChecks` EMF, without starting ingestion, fetching
provider/SEC data or sending SNS/SQS messages. It leaves one small S3 object and
three reserved DynamoDB rows for operator review/cleanup. See the
[finite cloud check](docs/DEPLOYMENT.md#finite-fargate-deployment-check) for saved
AWS CLI inputs, exact writes and prerequisites. Repeating an executed check needs
a new check ID. A failed-to-start task can retry with a fresh request token and
the same check ID after verifying that its fixtures are absent.

Normal execution contacts AWS, SEC and, when selected, Yahoo. It requires an identifying SEC User-Agent,
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
docker build -t finbot-ingestion:phase8 .
docker run --rm --network none finbot-ingestion:phase8 --help
docker run --rm --network none finbot-ingestion:phase8 --health-check
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
  finbot-ingestion:phase8
```

The example file is in Docker `--env-file` format. Passing it is explicit; the
application still reads process variables only. For local Docker, credentials must
be supplied explicitly through your chosen SDK credential mechanism. The CDK task definition uses AWS task-role credentials when deployed. Never bake credentials into the image.
No `FINBOT_DATA_ROOT` mount is used by this cloud ingestion service.

From another terminal:

```bash
docker exec finbot-ingestion python -m finbot_ingestion.main --health-check
docker stop --timeout 120 finbot-ingestion
```

SIGTERM/SIGINT stops poll producers and all new SEC and Yahoo HTTP admissions,
including authentication, retries and redirects. In-flight I/O and safe checkpoints can finish within the
existing cleanup procedure; queued SEC work remains recoverable. Bounded queues
drain within the configured grace, then remaining tasks cancel cooperatively. Blocking
HTTP/SDK calls finish before clients close. The default 30-second drain grace is
not a hard bound on total cleanup; Docker's stop timeout is separate. Forced
termination leaves interrupted work recoverable from durable checkpoints.

The image health check uses the local heartbeat every 30 seconds, with a five-second
command timeout, 120-second startup allowance and three retries. It checks liveness,
not full calendar readiness. Placeholder/stale calendar state remains visible in
health output and metrics. The CDK ECS health check uses a 300-second startup
allowance for its 150-second SEC quiet period; local startup defaults to no delay.

## Offline container validation

After rebuilding the image, run:

```bash
FINBOT_CONTAINER_TESTS=1 FINBOT_CONTAINER_IMAGE=finbot-ingestion:phase8 \
  .venv/bin/python -m pytest -q tests/integration/test_phase7.py -m container
```

Docker must be accessible from the execution environment. Opted-in tests fail if
Docker/the image is unavailable; ordinary pytest clearly skips these eight cases.
They mount a test-only startup shim and reusable stateful boundaries outside the
image, leaving the production entry point, configuration, real SEC limiter,
repositories and supervision intact. Containers have `--network none`, dummy
credentials, read-only filesystems and temporary fixture state. Tests cover
installed imports/dependencies, health, PID 1, SIGTERM/SIGINT, original bytes and
publication, blocked I/O cleanup and unexpected required-loop failure/return.
Yahoo cases exercise the installed factory through synthetic native-HTTP responses,
private writable caches and shutdown during a blocked calendar request.
No mock-mode switch is shipped in the application.

## Acquisition and durable contracts

Initial coverage is the [50-symbol production universe](docs/PRODUCTION_UNIVERSE.md),
with approximately 500 companies retained as the v0 capacity target.
Runtime company records require verified ticker/CIK/name/enabled fields.
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

Select `CALENDAR_PROVIDER=yahoo` to enable Yahoo observations for the enabled
curated ticker/CIK universe. Defaults are 30 inclusive calendar days, one refresh
daily after the previous refresh completes, and no near-term refresh. Dates use
America/New_York; a refresh on D covers D through D+29. An unexpired success that
still covers the polling window survives a midnight restart. Changed company
scope or insufficient scheduling coverage triggers a refresh.

The pinned yfinance transport collects raw day slices, checks query/schema/totals,
requires terminal pagination evidence and repeats each slice for consistency.
It uses its own single-worker executor, pacing and finite HTTP/row/page/deadline
budgets. This establishes validated observed collection, not independently verified
Yahoo coverage of every company. Malformed, capped or inconsistent collections
preserve expectations and the last successful freshness checkpoint.

Yahoo supplies scheduling hints, not cancellation authority. An empty or missing
observation preserves an existing expectation. Only an unambiguous date move with
the same provider, CIK and recognized quarterly-announcement title can replace an
old row. The replacement is strongly confirmed durable before guarded cancellation
of the unchanged old row. Unknown titles, multiple candidates and both dates in a
snapshot remain active. A versioned `replacement_hint` is matching evidence;
Yahoo event IDs remain null. Date moves do not inherit event satisfaction.
No EPS, revenue, estimates or market-cap data are persisted.

`CALENDAR_PROVIDER=placeholder` remains the default. It raises
`CalendarProviderNotConfigured`, never invents an empty snapshot and cannot
establish freshness. Authoritative fixture providers retain scoped omission-based
reconciliation. Full and near-term success scopes remain distinct.

Yahoo caches are private, process-local temporary state at `/tmp/finbot-yahoo` by
default. Keep that directory writable by the runtime user, including with a
read-only root filesystem. Cookies/crumbs never belong in DynamoDB, logs or images.
Only one Yahoo client owns yfinance cache state per process. Help and health-check
commands neither initialize Yahoo nor make provider requests.

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
The offline replay synchronizes virtual-time advances with worker wait boundaries
and includes a deliberately slower mocked SDK case so runner speed does not
determine whether polling and recovery complete in the simulated window.

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
alarms are defined by Phase 8 CDK and require manual infrastructure deployment.
CDK can provision an alarm SNS topic and email subscription using `alarm_email`
in ignored `infra/cdk/config.local.json`, or route to an existing `alarm_action_arn`.
Email subscriptions require recipient confirmation; `monitoring_enabled` controls
notification actions independently of ingestion. See [DEPLOYMENT.md](docs/DEPLOYMENT.md).

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
| Calendar | Provider `placeholder` or `yahoo` (default placeholder), lookahead/near-term 30/3 days, full/near-term refresh 86400/0 seconds, stale after 172800 seconds, maximum companies/events 1000/5000, company page 100, provider attempts 1 for Yahoo or 3 otherwise, checkpoint attempts 3, backoff 1/30 seconds |
| Yahoo | HTTP timeout 20 seconds, request spacing 1 second, page size 100, maximum pages per slice 20, HTTP attempts 300, raw rows 30000, whole-fetch deadline 600 seconds, request attempts 2, backoff cap 30 seconds, cache `/tmp/finbot-yahoo`; `YAHOO_*` names in example |
| Runtime | XNYS / America/New_York, active/safety 10/3600 seconds, reload/recovery/tick 300/60/1 seconds, refresh retry 60 seconds, poll/enumeration workers 2/1, company/filing/artifact queues 1000/100/200 |
| Runtime startup/metrics | `RUNTIME_SEC_STARTUP_QUIET_SECONDS=0` locally (150 in ECS), `RUNTIME_METRICS_ENVIRONMENT=local` (deployment environment in ECS) |
| Runtime health | Metrics/heartbeat 5/30 seconds, stall allowance 1800 seconds, shutdown grace 30 seconds, `RUNTIME_HEALTH_PATH=/tmp/finbot-ingestion-health.json` |

Durations must be finite and positive except zero disables near-term refresh
and is allowed for the SEC startup quiet period. Metrics environment must contain
1–64 letters, digits, underscores or hyphens.
Backoff cap must be at least base; active cadence cannot exceed safety cadence.
Runtime worker counts are bounded 1–16 and queues 1–10,000. Health path must be
absolute; derived reload coverage must fit the repository date-span bound.
Yahoo requires the America/New_York market timezone. Its aggregate fetch/retry
budget must fit below the runtime stall allowance with a 60-second margin.
SEC retries include network/timeouts, 403/429/5xx and package-index 404, with bounded
jitter and Retry-After. Redirects stay on official HTTPS SEC hosts and consume budget.

The repository encryption policy is
[ADR 010](docs/adr/010-use-service-managed-encryption-without-kms-integration.md):
bootstrap/raw-artifact buckets use SSE-S3, DynamoDB uses AWS-owned encryption,
and failed-work SQS uses SSE-SQS. SNS message bodies are intentionally unencrypted
at rest. HTTPS and scoped publication remain required. No project KMS keys,
aliases or application KMS permissions are needed.

Deployment must supply encrypted/HTTPS storage and least-privilege permissions:
conditional S3 PutObject, GetObject and scoped ListBucket for absence detection,
DynamoDB table/index access, SNS Publish and operational SQS SendMessage. Ingestion
must not delete or unconditionally replace raw objects. Resources/policies/retention
are defined in Phase 8 CDK; no runtime adapter provisions them.

## Design documents and legacy recovery

[HLD](docs/HLD.md), [LLD](docs/LLD.md), [ADRs](docs/adr/) and
[MIGRATION_PLAN](docs/MIGRATION_PLAN.md) remain the sources of truth.
[BACKLOG](docs/BACKLOG.md) tracks unscheduled future changes and revisit triggers.
[ADR 008](docs/adr/008-use-conservative-ecs-automatic-recovery.md) records the
implemented Phase 8 recovery policy and its earnings-latency tradeoff.
[DEPLOYMENT](docs/DEPLOYMENT.md) documents CDK, GitHub configuration, readiness,
stop/wait/start releases and recovery; [PHASE_8_PLAN](docs/PHASE_8_PLAN.md) records
delivery and validation.
[DEPLOYMENT_WALKTHROUGH](docs/DEPLOYMENT_WALKTHROUGH.md) explains the first AWS
deployment step by step, including each stack, GitHub's role and activation controls.
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
remain outside v0. Next milestones: ongoing provider access,
full 30-day live validation, separately
authorized cloud application checks, alarm routing and production activation.

## Phase 8 infrastructure and delivery

Install the independent pinned CDK toolchain and synthesize fixture templates
without AWS access:

```bash
python3.12 -m venv infra/cdk/.venv
infra/cdk/.venv/bin/python -m pip install -r infra/cdk/requirements-dev.txt
npm ci --ignore-scripts --no-audit --no-fund
infra/cdk/.venv/bin/python -m pytest -q infra/cdk/tests
npm run synth -- --no-lookups
```

Use [DEPLOYMENT.md](docs/DEPLOYMENT.md) for real configuration and deployment;
never deploy the default dummy inputs. CDK owns retained state and a runtime
service at desired count zero. GitHub CI has no AWS credentials; delivery uses
scoped OIDC, immutable tags/digests and an explicit STOPPED handoff. Stopped
environments stay stopped until separate readiness approval and manual activation.
The customized account/region bootstrap template is version-controlled under
[infra/bootstrap](infra/bootstrap/README.md), with its pinned origin, SSE-S3
policy, explicit deployment parameters and update procedure. Bootstrap remains a separately
authorized manual prerequisite to deploying the application stacks.
Pinned local CloudFormation schema and encryption-policy checks, their standard
commands and reviewed warnings are documented in
[infra/validation](infra/validation/README.md).
GitHub production-environment settings and non-secret deployment variables are saved under
[infra/github](infra/github/README.md), applied with the standard `gh` CLI.
For Yahoo, set `calendar_provider` to `yahoo` in the CDK JSON configuration.
It emits the explicit provider, 30-day/daily defaults,
one outer attempt and a writable `/tmp` cache path; desired count stays zero.
See [YAHOO_CALENDAR_PLAN](docs/YAHOO_CALENDAR_PLAN.md) and
[ADR 009](docs/adr/009-use-yahoo-calendar-observations-with-replacement-only-reconciliation.md)
for delivery, reconciliation and remaining live-validation limits.

Automatic ECS recovery uses min/max 0/100, a 120-second stop timeout and a
150-second quiet period before SEC traffic. This is conservative protection,
not formal cross-process exclusion. The quiet period alone adds 2.5 minutes to
recovery and can substantially delay active-window earnings discovery; follow
[ARCH-001](docs/BACKLOG.md#arch-001--reduce-recovery-delay-during-active-earnings-windows).
