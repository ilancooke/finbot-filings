# finbot SEC ingestion

`finbot-filings` is a focused SEC document-acquisition service. Its installed
Python namespace is `finbot_ingestion`. It discovers relevant filings, acquires
original document bytes, stores immutable artifacts in S3, maintains DynamoDB
metadata/checkpoints and publishes `ArtifactReady` events to SNS. Document
interpretation, earnings extraction, features and consumer queues belong downstream.

The migration from the POC and the subsequent earnings-focused filtering work
are complete. Only `finbot_ingestion` is installed. Infrastructure and application
delivery are in place. The [initial production universe](docs/PRODUCTION_UNIVERSE.md)
contains 50 reviewed companies; approximately 500 remains the capacity target.

See [architecture and contracts](docs/ARCHITECTURE.md),
[accepted decisions](docs/DECISIONS.md), and the
[deployment and recovery runbook](docs/DEPLOYMENT.md). The runbook preserves a
short dated verification record; [BACKLOG](docs/BACKLOG.md) tracks remaining
operational observations and deferred work.

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

## Test organization

Tests are grouped by current responsibility rather than migration phase:

- `tests/unit/`: configuration, domain contracts, SEC parsing/transport, selection,
  scheduling, execution, observability and operator helpers.
- `tests/integration/`: repositories and SDK wire contracts, ingestion/recovery,
  calendar synchronization, runtime behavior, SEC package replay and capacity replay.
- `tests/support/`: reusable fakes, builders and runtime setup; test modules do not
  import other test modules. Shared integration fixtures are registered in
  `tests/integration/conftest.py`.
- `tests/container/`: mounted subprocess/container harness; never installed in the
  production image. The eight opt-in cases live in
  `tests/integration/test_container_lifecycle.py`.
- `infra/cdk/tests/`: independent infrastructure and workflow checks, run with the
  CDK virtual environment.

The application network guard and dummy AWS credentials are centralized in
`tests/conftest.py`. Subprocess/container and infrastructure guards remain independent.
Yahoo no-backoff fixtures apply only to their synthetic-boundary consumers.
Tests can run individually, for example:

```bash
.venv/bin/python -m pytest -q tests/integration/test_ingestion_pipeline.py
.venv/bin/python -m pytest -q tests/unit/test_earnings_satisfaction_policy.py
.venv/bin/python -m pytest -q -s tests/integration/test_runtime_capacity.py
```

## Supported commands

```bash
.venv/bin/python -m finbot_ingestion.main --help
.venv/bin/python -m finbot_ingestion.main
# Separate process; checks only the local heartbeat:
.venv/bin/python -m finbot_ingestion.main --health-check
# Separately launched Fargate task only; finite AWS check with isolated test writes:
.venv/bin/python -m finbot_ingestion.main --deployment-check --check-id readiness-new-1
# One-shot live Yahoo collection check; no AWS calls, SEC calls or runtime startup:
.venv/bin/python -m finbot_ingestion.calendar.check \
  --companies-file infra/seed/companies.prod.json \
  --days 30 \
  --output /private/tmp/finbot-calendar-check-new.json
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

The runtime reads process environment variables. Production receives them through
the CDK-managed ECS task definition and uses Python defaults for optional settings;
see the [configuration reference](#configuration-reference). No `.env` files are
needed for production or offline tests, and the application does not load them.
`LOG_LEVEL` defaults to `INFO`. AWS uses the normal SDK credential chain, including
task-role credentials in ECS. Do not place credentials in source, images or
diagnostic payloads.

## Container workflow

Build again after application, dependency or image-configuration changes:

```bash
docker build -t finbot-ingestion:local .
docker run --rm --network none finbot-ingestion:local --help
docker run --rm --network none finbot-ingestion:local --health-check
```

The final command returns one because that new container has no runtime heartbeat.
The image installs a wheel in Python 3.12 slim Linux, runs as UID/GID 10001, and
starts `python -m finbot_ingestion.main` directly as PID 1. It contains runtime
dependencies and timezone data; tests, build sources, credentials and shared data
are excluded. It exposes no inbound port. `/tmp` holds the atomic heartbeat.

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
FINBOT_CONTAINER_TESTS=1 FINBOT_CONTAINER_IMAGE=finbot-ingestion:local \
  .venv/bin/python -m pytest -q tests/integration/test_container_lifecycle.py -m container
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
without a count cutoff, then admission applies `earnings-acquisition-v2`:

- `LIVE_INGESTION_START_AT` defaults to **2026-10-06T04:00:00Z**, inclusive, using
  SEC acceptance time (October 6 midnight in America/New_York). Keep this fixed
  across restarts/deployments and newly enabled companies; it is not a rolling lookback or today's date.
- 8-K/8-K/A require unambiguous Item 2.02 evidence. Known non-earnings filings are
  skipped. Missing evidence may be resolved from index metadata or structural
  primary-report headings; unresolved/conflicting evidence stays visible.
- 10-Q/10-K and their amendments retain their primary financial report.

Identical accession duplicates collapse; conflicts and invalid relevant rows reject
parsing. A missing primary name can be resolved by package enumeration. Historical
backfill tooling and traversal beyond the recent response are deferred. Do not
reset/widen the live cutoff to perform an incidental backfill.

Package discovery reconciles the HTML document table and directory JSON, preserving
exhibit descriptions. Selected earnings 8-Ks retain the primary plus identified
releases and financial supplements in HTML/text/PDF form. Descriptions and safe
primary-report exhibit references determine roles; exhibit number alone does not.
Routing also recognizes descriptive same-package links outside exhibit tables,
generic news/press releases and quarterly supplements tied to exact Item 2.02
exhibit references, and explicitly labelled financial statements/supplementary
sales data. See the [exact attachment rules](docs/ARCHITECTURE.md#filing-admission-and-document-selection).
Decks, standalone images/logos, complete submission TXT bundles, unrelated exhibits
and auxiliary files are skipped without downloading them. Legitimate selected text
releases are allowed. Unresolved exhibit roles are logged and skipped while clear
selected documents proceed. Image-only HTML receives a coverage warning when the
bounded text inspection detects it; no linked images, OCR or financial extraction.
Primary routing/text inspection is limited to 4 MiB; larger/unsupported layouts
stay unresolved. Acquisition still permits selected documents up to 64 MiB.
Completed packages are not automatically re-enumerated when routing changes;
previously omitted attachments require a separate scoped recovery operation.
The operator-only `scripts/recover_earnings_attachments.py` previews and queues
the four reviewed DAL/WFC/JNJ omissions. See the
[targeted recovery steps](docs/DEPLOYMENT.md#targeted-dalwfcjnj-attachment-recovery).

Missing/inconsistent snapshots are retried rather than checkpointed as complete.
Enumeration completion counts selected children for new packages. Existing completed
snapshot timestamps/counts remain unchanged. Automatic completed-package rechecking
remains deferred. Primary bytes fetched for routing use the same immutable storage
path instead of a second download.

Artifact IDs are `<dashed-accession>/<original-filename>`; filenames preserve case
and must be safe basenames. S3 keys are
`<ticker>/<YYYY-MM-DD>_<form>_<accession>/<original-filename>`. The folder date is
the SEC acceptance day in America/New_York, not discovery time;
amendment form labels replace `/` with `-` (for example `10-K-A`). The canonical
artifact ticker is retained across rediscovery. Ticker folders allow letters,
digits, dots and hyphens and must start with a letter or digit. CIK/accession
remain stable identities. S3 URI paths are percent-encoded; consumers decode the
URI path to obtain the original key.
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

GSIs use KEYS_ONLY projection. See
[DynamoDB contracts](docs/ARCHITECTURE.md#dynamodb-schema-and-access-patterns) for exact schema,
revision guards, scoped pagination, sync/satisfaction records and compatibility.
The Calendar table must permit reserved `__calendar_sync__` and
`__event_satisfaction__` partitions. Inactive cancellation tombstones prevent stale
refreshes from restoring cancelled events. Do not run superseded adapter versions
against newer processing/calendar records.

Conditional creates and guarded updates preserve first observations/checkpoints.
Enumeration completes only after every child is confirmed durable through a strong
base-table read. Transient queue/download states remain in memory. Pending sparse
indexes drive repeated recovery without scans or TTL, including work from disabled
companies. The same date/filing/document filters guard pending acquisition and
publication, including legacy rows. A small selection checkpoint removes excluded
or unresolved work from pending indexes without inventing success/failure facts.
Stored bytes, publication history and terminal/dead-letter obligations remain intact.
GSIs are eventual; candidates are strongly rechecked.
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

After the first durable earnings match, frequent polling continues for ten minutes
at the existing active cadence to discover companion reports and earnings releases.
The fixed deadline is the recorded `satisfied_at` plus ten minutes; later filings,
calendar reloads and restarts do not extend it. A restart resumes only the time
remaining. This follow-up applies even outside the original polling window. After
it expires, hourly safety polling resumes unless another unsatisfied expectation
requires active polling. The shared SEC request ceiling and retry backoff still apply.

All SEC polls/indexes/downloads/retries/redirects share a five-request/second ceiling.
One HTTP attempt is in flight at a time through reused synchronous transport and a
bounded executor. Slow responses and competing work reduce throughput. Target poll
cadence and downstream latency are not unconditional guarantees. See the
[capacity limits and replay command](docs/ARCHITECTURE.md#observability-deployment-and-capacity-limits).
The offline replay synchronizes virtual-time advances with worker wait boundaries
and includes a deliberately slower mocked SDK case so runner speed does not
determine whether polling and recovery complete in the simulated window.

Version `earnings-satisfaction-v2` records a durable satisfaction checkpoint for
the same CIK and SEC acceptance on the expected date
in `America/New_York`: midnight inclusive to the following midnight exclusive.
Matching is independent of polling windows and provider before/after-market timing,
so early-morning earnings filings can match. There is no matching grace beyond that
date. Exact original `10-Q` and `10-K` qualify; original `8-K` also requires
unambiguous submissions Item `2.02` metadata. Amendments/generic or ambiguous 8-Ks
remain ingested without satisfying the event. Ambiguous expectations and reuse of
an already matched accession are rejected. Existing `earnings-satisfaction-v1`
checkpoints remain valid and are not rewritten. Normal SEC polls reevaluate already
downloaded filings for loaded calendar expectations without downloading them again.
Safety polling and pending recovery continue. This is a scheduling heuristic, not
evidence of extracted or validated earnings. See the
[matching policy rollout](docs/DEPLOYMENT.md#earnings-matching-policy-updates).

Required-loop failure, unexpected return or stalled busy work exits nonzero.
Sanitized JSON logs go to stderr; bounded CloudWatch EMF records go to stdout with
Service/Environment dimensions; `SecRequests` also has company and call-type
breakdowns described below. Local health separates liveness from calendar
age/scope/provider configuration. CloudWatch collection, DLQ-depth monitoring and
alarms are defined by CDK and require manual infrastructure deployment.
CDK can provision an alarm SNS topic and email subscription using `alarm_email`
in ignored `infra/cdk/config.local.json`, or route to an existing `alarm_action_arn`.
Email subscriptions require recipient confirmation; `monitoring_enabled` controls
notification actions independently of ingestion. See [DEPLOYMENT.md](docs/DEPLOYMENT.md).

### SEC request breakdowns in CloudWatch

`Finbot/Ingestion` / `SecRequests` counts each dispatched SEC HTTP attempt,
including unsuccessful attempts, retries and redirect hops. The existing total
keeps its `Service=finbot-ingestion`, `Environment=prod` dimensions. Additional
series provide independent and combined views:

| View | Additional dimensions |
| --- | --- |
| Total | None |
| By company | `Ticker` |
| By call type | `CallType` |
| By company and call type | `Ticker`, `CallType` |

Call types are `submissions` (company filing polls), `filing_index` (HTML listing),
`filing_directory` (JSON listing), and `document` (primary filings/exhibits).
Retries/redirects retain their original ticker and call type. Both document
download paths carry company context. Direct client calls without company context
use `Ticker=UNKNOWN`, so unattributed requests remain included in each breakdown.

In region `us-east-1`, open **CloudWatch → Metrics → All metrics →
Finbot/Ingestion**. Select the dimension group for the desired view, filter to
production and `SecRequests`, and select the series to graph. Under **Graphed
metrics**, choose **Sum** and a **5-minute** period; a stacked graph shows the
contributions to each period's total. For example, a total of seven attempts can
break down into two submissions, two HTML indexes, two JSON directories and one
document. Company totals and company/type totals each sum to those same seven.
Use one view at a time when summing series; adding multiple views double-counts
the same attempts.

All views share one atomic counter snapshot and EMF flush timestamp. Series are
emitted only when requests occur; these are HTTP attempt counts, not successful
responses or distinct companies polled. In particular, filter `CallType=submissions`
in the combined view to examine company polling attempts. Breakdowns start with
the release that adds them; older aggregate datapoints cannot be split afterward.
Each distinct dimension combination is a billable custom metric. URLs, CIKs,
accessions and filenames are not metric dimensions. Existing alarms and other
metrics retain their current dimensions.

The raw EMF log stream contains both the total and breakdown records. To query
only total request counts in Logs Insights QL, select `/finbot/finbot-prod` and use:

```sql
filter Service = "finbot-ingestion" and Environment = "prod"
    and ispresent(SecRequests) and not ispresent(Ticker)
| stats sum(SecRequests) as requests by bin(5m)
```

## Configuration reference

Production environment values come from the CDK-managed ECS task definition.
CDK reads tracked production settings from `infra/cdk/config.prod.json`, selected
by `FINBOT_INFRA_CONFIG`. `FINBOT_INFRA_PRIVATE_CONFIG` selects the ignored
`infra/cdk/config.local.json`, containing only `account`, `sec_user_agent`, and
optional `alarm_email`. Private inputs cannot override the cutoff, image digest,
or other deployment settings. See [DEPLOYMENT](docs/DEPLOYMENT.md) for commands.
Default offline synthesis uses `config.example.json` with
`config.private.example.json`, mirroring the production split with dummy values.
Optional runtime settings use Python defaults when absent from the process
environment. Explicit environment values take precedence. The tables below
document required settings and optional defaults without requiring an environment
file.

Configuration parsing does not
create clients, resolve credentials or load files implicitly. Region/table/storage/
messaging requirements apply to normal runtime execution; isolated domain/parser
utilities need only their own relevant settings.

| Group | Required settings / defaults |
| --- | --- |
| SEC | Required `SEC_USER_AGENT`; `SEC_MAX_REQUESTS_PER_SECOND=5` (finite, positive, at most 5) |
| SEC transport | `SEC_CONNECT_TIMEOUT_SECONDS=10`, `SEC_READ_TIMEOUT_SECONDS=30`, `SEC_MAX_ATTEMPTS=3`, `SEC_BACKOFF_BASE_SECONDS=1`, `SEC_BACKOFF_CAP_SECONDS=30`, `SEC_MAX_REDIRECTS=5` |
| AWS/DynamoDB | Required `AWS_REGION`, four distinct `COMPANIES_TABLE`, `CALENDAR_TABLE`, `FILINGS_TABLE`, `ARTIFACTS_TABLE` names |
| DynamoDB execution | Connect/read timeouts 5/10 seconds, 3 attempts, page size 100, 4 workers, 4 CAS attempts, maximum date span 366 days; `DYNAMODB_*` names below |
| Storage/messaging | Required `ARTIFACT_BUCKET`, same-region standard `ARTIFACT_READY_TOPIC_ARN`, standard `INGESTION_DEAD_LETTER_QUEUE_URL`; `MAX_ARTIFACT_BYTES=67108864` |
| Ingestion | AWS connect/read 5/30 seconds, attempts/workers 3/2, stage failures 3, checkpoint/dead-letter attempts 3/3, inflight artifacts 2, recovery page 100, backoff 1/30 seconds; `INGESTION_*` names below |
| Calendar | Provider `placeholder` or `yahoo` (default placeholder), lookahead/near-term 30/3 days, full/near-term refresh 86400/0 seconds, stale after 172800 seconds, maximum companies/events 1000/5000, company page 100, provider attempts 1 for Yahoo or 3 otherwise, checkpoint attempts 3, backoff 1/30 seconds |
| Yahoo | HTTP timeout 20 seconds, request spacing 1 second, page size 100, maximum pages per slice 20, HTTP attempts 300, raw rows 30000, whole-fetch deadline 600 seconds, request attempts 2, backoff cap 30 seconds, cache `/tmp/finbot-yahoo`; `YAHOO_*` names below |
| Runtime | XNYS / America/New_York, active/safety 10/3600 seconds, reload/recovery/tick 300/60/1 seconds, refresh retry 60 seconds, poll/enumeration workers 2/1, company/filing/artifact queues 1000/100/200 |
| Runtime startup/metrics | `RUNTIME_SEC_STARTUP_QUIET_SECONDS=0` locally (150 in ECS), `RUNTIME_METRICS_ENVIRONMENT=local` (deployment environment in ECS) |
| Runtime health | Metrics/heartbeat 5/30 seconds, stall allowance 1800 seconds, shutdown grace 30 seconds, `RUNTIME_HEALTH_PATH=/tmp/finbot-ingestion-health.json` |

<details>
<summary>Advanced optional settings and their Python defaults</summary>

These optional settings can be omitted from the process environment. Supply a value
only when overriding its default. ECS explicitly overrides startup quiet time to
150 seconds and metrics environment to the deployed environment.

```dotenv
SEC_MAX_REQUESTS_PER_SECOND=5
SEC_CONNECT_TIMEOUT_SECONDS=10
SEC_READ_TIMEOUT_SECONDS=30
SEC_MAX_ATTEMPTS=3
SEC_BACKOFF_BASE_SECONDS=1
SEC_BACKOFF_CAP_SECONDS=30
SEC_MAX_REDIRECTS=5
DYNAMODB_CONNECT_TIMEOUT_SECONDS=5
DYNAMODB_READ_TIMEOUT_SECONDS=10
DYNAMODB_MAX_ATTEMPTS=3
DYNAMODB_PAGE_SIZE=100
DYNAMODB_MAX_WORKERS=4
DYNAMODB_CAS_ATTEMPTS=4
DYNAMODB_MAX_CALENDAR_RANGE_DAYS=366
MAX_ARTIFACT_BYTES=67108864
INGESTION_AWS_CONNECT_TIMEOUT_SECONDS=5
INGESTION_AWS_READ_TIMEOUT_SECONDS=30
INGESTION_AWS_MAX_ATTEMPTS=3
INGESTION_AWS_MAX_WORKERS=2
INGESTION_MAX_STAGE_FAILURES=3
INGESTION_CHECKPOINT_ATTEMPTS=3
INGESTION_DEAD_LETTER_ATTEMPTS=3
INGESTION_MAX_INFLIGHT_ARTIFACTS=2
INGESTION_RECOVERY_PAGE_SIZE=100
INGESTION_BACKOFF_BASE_SECONDS=1
INGESTION_BACKOFF_CAP_SECONDS=30
CALENDAR_LOOKAHEAD_DAYS=30
CALENDAR_NEAR_TERM_DAYS=3
CALENDAR_FULL_REFRESH_SECONDS=86400
CALENDAR_NEAR_TERM_REFRESH_SECONDS=0
CALENDAR_STALE_AFTER_SECONDS=172800
CALENDAR_MAX_COMPANIES=1000
CALENDAR_MAX_SNAPSHOT_EVENTS=5000
CALENDAR_COMPANY_PAGE_SIZE=100
CALENDAR_CHECKPOINT_ATTEMPTS=3
CALENDAR_BACKOFF_BASE_SECONDS=1
CALENDAR_BACKOFF_CAP_SECONDS=30
YAHOO_HTTP_TIMEOUT_SECONDS=20
YAHOO_MIN_REQUEST_INTERVAL_SECONDS=1
YAHOO_PAGE_SIZE=100
YAHOO_MAX_PAGES_PER_SLICE=20
YAHOO_MAX_HTTP_ATTEMPTS=300
YAHOO_MAX_RAW_ROWS=30000
YAHOO_MAX_FETCH_SECONDS=600
YAHOO_REQUEST_ATTEMPTS=2
YAHOO_BACKOFF_CAP_SECONDS=30
YAHOO_CACHE_DIR=/tmp/finbot-yahoo
RUNTIME_MARKET_TIMEZONE=America/New_York
RUNTIME_MARKET_CALENDAR=XNYS
RUNTIME_ACTIVE_POLL_SECONDS=10
RUNTIME_SAFETY_POLL_SECONDS=3600
RUNTIME_BEFORE_OPEN_SECONDS=7200
RUNTIME_AFTER_OPEN_SECONDS=7200
RUNTIME_BEFORE_CLOSE_SECONDS=7200
RUNTIME_AFTER_CLOSE_SECONDS=10800
RUNTIME_GRACE_SECONDS=7200
RUNTIME_NON_SESSION_START_HOUR=7
RUNTIME_NON_SESSION_START_MINUTE=30
RUNTIME_NON_SESSION_END_HOUR=19
RUNTIME_RELOAD_SECONDS=300
RUNTIME_RECOVERY_SECONDS=60
RUNTIME_TICK_SECONDS=1
RUNTIME_REFRESH_RETRY_SECONDS=60
RUNTIME_POLL_WORKERS=2
RUNTIME_ENUMERATION_WORKERS=1
RUNTIME_COMPANY_QUEUE_SIZE=1000
RUNTIME_FILING_QUEUE_SIZE=100
RUNTIME_ARTIFACT_QUEUE_SIZE=200
RUNTIME_METRICS_FLUSH_SECONDS=5
RUNTIME_HEARTBEAT_SECONDS=30
RUNTIME_STALL_SECONDS=1800
RUNTIME_SHUTDOWN_GRACE_SECONDS=30
RUNTIME_SEC_STARTUP_QUIET_SECONDS=0
RUNTIME_METRICS_ENVIRONMENT=local
RUNTIME_HEALTH_PATH=/tmp/finbot-ingestion-health.json
```

</details>

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
[ADR 010](docs/DECISIONS.md#adr-010):
bootstrap/raw-artifact buckets use SSE-S3, DynamoDB uses AWS-owned encryption,
and failed-work SQS uses SSE-SQS. SNS message bodies are intentionally unencrypted
at rest. HTTPS and scoped publication remain required. No project KMS keys,
aliases or application KMS permissions are needed.

Deployment must supply encrypted/HTTPS storage and least-privilege permissions:
conditional S3 PutObject, GetObject and scoped ListBucket for absence detection,
DynamoDB table/index access, SNS Publish and operational SQS SendMessage. Ingestion
must not delete or unconditionally replace raw objects. Resources/policies/retention
are defined in CDK; no runtime adapter provisions them.

## Design documents and legacy recovery

- [ARCHITECTURE](docs/ARCHITECTURE.md): current scope and durable contracts.
- [DECISIONS](docs/DECISIONS.md): numbered decisions, rationale and revisit triggers.
- [DEPLOYMENT](docs/DEPLOYMENT.md): operator procedures and dated verification.
- [BACKLOG](docs/BACKLOG.md): unresolved investigations and deferred scope.
- [PRODUCTION_UNIVERSE](docs/PRODUCTION_UNIVERSE.md): reviewed initial company input.

Live ingestion is distinct from deferred historical backfill
([ADR 011](docs/DECISIONS.md#adr-011)); earnings selection follows
[ADR 012](docs/DECISIONS.md#adr-012). Rollback must use an image compatible with
selection checkpoints. See [filtering compatibility](docs/DEPLOYMENT.md#filtering-compatibility).
Migration/phase plans and detailed delivery history are retired from the maintained
docs tree and recoverable in Git. Current documentation describes the completed
service; routine test/release evidence belongs in CI and release artifacts.

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
remain outside the current service. Backfill and operator terminal redrive remain
deferred in BACKLOG.

## Infrastructure and delivery

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
Delivery settles a new task revision at desired count zero before separately
scaling to one; restoration follows the same sequence.
The customized account/region bootstrap template is version-controlled under
[infra/bootstrap](infra/bootstrap/README.md), with its pinned origin, SSE-S3
policy, explicit deployment parameters and update procedure. Bootstrap remains a separately
authorized manual prerequisite to deploying the application stacks.
Pinned local CloudFormation schema and encryption-policy checks, their standard
commands and reviewed warnings are documented in
[infra/validation](infra/validation/README.md).
GitHub production-environment settings and non-secret deployment variables are saved under
[infra/github](infra/github/README.md), applied with the standard `gh` CLI.
For Yahoo, set `calendar_provider` to `yahoo` in the tracked CDK JSON configuration.
It emits the explicit provider, 30-day/daily defaults,
one outer attempt and a writable `/tmp` cache path; desired count stays zero.
See [Yahoo contracts](docs/ARCHITECTURE.md#yahoo-calendar-synchronization) and
[ADR 009](docs/DECISIONS.md#adr-009) for reconciliation and coverage limits.

Automatic ECS recovery uses min/max 0/100, a 120-second stop timeout and a
150-second quiet period before SEC traffic. This is conservative protection,
not formal cross-process exclusion. The quiet period alone adds 2.5 minutes to
recovery and can substantially delay active-window earnings discovery; follow
[ARCH-001](docs/BACKLOG.md#arch-001--recovery-delay).

## Readable storage layout and acquisition reset

Current objects use, for example,
`DAL/2026-10-09_10-Q_0000027904-26-000038/dal-20260930.htm`. Storage provenance
version 2 includes the canonical SEC acceptance timestamp. Earlier CIK-only
layouts are not migrated or searched as a fallback. A layout transition needs the
explicit stopped-service reset in [DEPLOYMENT](docs/DEPLOYMENT.md#explicit-acquisition-reset).
The operator-only `scripts/reset_acquisition.py` inventories by default; it is
never part of normal ingestion or container startup. See [ADR 013](docs/DECISIONS.md#adr-013).
