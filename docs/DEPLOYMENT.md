# Deployment and recovery runbook

Use [README](../README.md) for local commands/settings,
[ARCHITECTURE](ARCHITECTURE.md) for durable contracts and
[DECISIONS](DECISIONS.md) for policy rationale. Commands that deploy, publish, seed,
launch, change alarms or delete diagnostic fixtures affect real systems; execute
only for the reviewed target and authorized operation. Offline validation does
not itself activate a service. Historical plans and deployment chronology remain
recoverable in Git.

## Deployment ownership

CDK manually provisions shared bootstrap, retained state and runtime. GitHub
builds/tests/delivers application images through scoped OIDC. The service is a
background worker with outbound SEC/Yahoo access and no inbound API.

| Owner | Responsibility |
| --- | --- |
| `CDKToolkit` | Shared deployment asset bucket/repository, five roles and bootstrap version |
| State stack | Raw S3, four tables/indexes, ArtifactReady SNS, failed-work SQS, application ECR |
| Runtime stack | Network, cluster/service, baseline task definition, roles, logs and alarms |
| GitHub release | Digest-pinned application revisions matching baseline configuration except image |
| Operator | Reviewed configuration, company inputs, readiness, approval and infrastructure maintenance |

Task role accesses application data; execution role pulls images/writes logs;
release role publishes images/updates this service. Release cannot deploy CDK,
mutate IAM/data or launch standalone tasks. Bootstrap assets are separate from
application artifact storage/ECR. Baseline and application revisions have different
owners; do not change the baseline variable to each GitHub-created revision.

## Earnings matching policy updates

The ten-minute post-match frequent-polling extension also uses normal application
delivery, with no CDK, alarm, environment or data-schema changes. It derives its
deadline from the existing durable `satisfied_at` timestamp. Restarts resume only
the remainder; matches older than ten minutes stay on safety polling unless an
unsatisfied expectation requires active polling. `ActiveCompanies` includes
companies in this follow-up period. Calendar matches remain visible immediately;
follow-up does not wait for document storage or notification publication.

The `earnings-satisfaction-v2` application change requires no CDK deployment,
environment changes, table migration or acquisition reset. Update/restart
`finbot-dashboard` with the v1/v2 checkpoint reader before releasing the ingestion
image through normal CI/application delivery. V1 checkpoints remain valid.

After release, normal SEC polls reevaluate existing filings for loaded calendar
expectations. Early same-date qualifying filings can acquire v2 satisfaction
checkpoints without downloading or publishing their documents again. Verify the
previously unlinked expectations in the dashboard after a poll and **Refresh**:
the calendar should show **Filing found** alongside existing download totals.
Missing/ambiguous SEC Item 2.02 evidence or overlapping expectations still prevent
a match. Dates outside the loaded calendar lookback are not historical backfilled.

Once v2 checkpoints exist, an older ingestion image that only understands v1 is
not a compatible rollback target. Preserve both readers in any rollback build;
do not delete satisfaction records to make an older image start.

## Configuration and offline validation

Application setup is in [README](../README.md#install-and-validate). Install the
independent pinned CDK toolchain with Python 3.12+ and Node 24:

```bash
python3.12 -m venv infra/cdk/.venv
infra/cdk/.venv/bin/python -m pip install -r infra/cdk/requirements-dev.txt
npm ci --ignore-scripts --no-audit --no-fund
infra/cdk/.venv/bin/python -m pytest -q infra/cdk/tests
npm run synth -- --no-lookups
```

Default synthesis uses the dummy pair `infra/cdk/config.example.json` and
`infra/cdk/config.private.example.json`; never deploy those inputs. They mirror
the production public/private structure, with fictitious account, contacts,
GitHub subject, and image digest. The example environment is named `example`.
Production settings (including the approved image digest and live cutoff) are
tracked in `infra/cdk/config.prod.json`. Copy `infra/cdk/config.private.example.json`
to ignored `infra/cdk/config.local.json` and supply the real account and contact
details. The private file permits only `account`, `sec_user_agent`, and optional
`alarm_email`; it cannot override tracked deployment settings. Existing local
files containing all settings must be split before using this configuration.

From the repository root, select both files explicitly:

```bash
export FINBOT_INFRA_CONFIG="$PWD/infra/cdk/config.prod.json"
export FINBOT_INFRA_PRIVATE_CONFIG="$PWD/infra/cdk/config.local.json"
```

Both files are JSON inputs, not `cdk -c` context. Unset both variables for the
default offline fixture synthesis. Real private input is read only when explicitly
selected; missing or invalid private input fails. Keep account/contact values out
of new public configuration. Existing historical account references elsewhere in
the repository remain unchanged by this configuration split.
On restricted macOS use `JSII_RUNTIME_PACKAGE_CACHE=/private/tmp/finbot-jsii`.
Strict synthesis is credential-free with no AWS lookups. Run the pinned local
schema/policy checks in [infra/validation](../infra/validation/README.md).

| Input | Meaning/default |
| --- | --- |
| `account`, `region`, `prefix`, `environment` | Explicit target/names; environment identifies metrics |
| `sec_user_agent` | Identifying contact in ignored local configuration |
| `github_subject` | Exact protected production-environment OIDC subject |
| `github_oidc_provider_arn` | Optional existing provider to import |
| `image_digest` | Validated Linux ARM64 ECR `sha256:...` digest |
| `cpu`, `memory_mib` | Initially 512/1024; supported Fargate combinations |
| `calendar_provider` | `yahoo` or default unavailable `placeholder` |
| `calendar_lookahead_days` | 30 inclusive dates; validated 3–360 |
| `calendar_full_refresh_seconds`, `calendar_near_term_refresh_seconds` | 86400 / 0 after completion |
| `yahoo_cache_dir` | Private writable `/tmp/finbot-yahoo` |
| `live_ingestion_start_at` | Fixed inclusive `2026-10-06T04:00:00Z` |
| `alarm_email`, `alarm_action_arn` | Mutually exclusive managed email/existing topic |
| `monitoring_enabled` | Notification actions; default false, independent of ingestion |
| `sec_error_threshold`, `publish_error_threshold` | Initially 10 / 3 per minute |
| `discovery_latency_ms`, `failed_work_age_seconds` | Initially 60000 / 300 |

Read the actual GitHub OIDC subject before configuring trust:

```bash
gh api repos/ilancooke/finbot-filings/actions/oidc/customization/sub
```

Append `:environment:production` to the returned prefix, including permanent IDs
when immutable subjects are enabled. `use_default=true` does not imply name-only
subjects. Recorded production subject:
`repo:ilancooke@8453151/finbot-filings@1341430877:environment:production`.
Trust also requires `aud=sts.amazonaws.com`. Protect production with required
reviewer/main-only branch policy. No static AWS keys belong in variables/images.

Supported VPC: two public subnets (region suffixes a/b), public task IP, HTTPS-only
egress, no ingress/NAT/load balancer. Verify target AZ availability; private egress
needs reviewed changes. Retain data/images, stack termination protection, S3
conditional writes/versioning, DynamoDB on-demand/PITR/deletion protection, 14-day
SSE-SQS failed-work retention and 30-day logs. ECR tags are immutable; preserve
rollback images. Encryption follows [ADR 010](DECISIONS.md#adr-010). RETAIN does
not make construct renaming/resource replacement safe.

## Provision a new environment

1. Confirm operator AWS identity/account/region. Keep delivery/activation flags
   false until configuration and readiness are reviewed.
2. Review [bootstrap](../infra/bootstrap/README.md) and the customized template.
   Bootstrap is a separate shared account/region operation:

   ```bash
   npm run cdk -- bootstrap aws://ACCOUNT/REGION \
     --template infra/bootstrap/bootstrap-template.yaml \
     --profile default --termination-protection \
     --cloudformation-execution-policies arn:aws:iam::aws:policy/AdministratorAccess
   ```

   Always pass the saved template: version 32, qualifier `hnb659fds`, variant
   `Finbot: SSE-S3 v1`. CLI upgrades need upstream comparison.
3. Configure/synthesize/review state, then deploy it:

   ```bash
   npm run cdk -- diff PREFIX-ENV-state --method template --profile default --no-lookups
   npm run cdk -- deploy PREFIX-ENV-state --exclusively --profile default
   ```

4. Build/test the Linux ARM64 image using [container checks](../README.md#offline-container-validation).
   Publish an immutable tag to application ECR with `--provenance=false` for the
   single-platform manifest; record registry digest and scan findings.
5. Save the digest in tracked production config, review/deploy runtime using the same pattern.
   Runtime CDK leaves desired zero. Record outputs and actual stopped task state.
6. Configure GitHub via [infra/github](../infra/github/README.md), company inputs,
   diagnostics/notifications and readiness before activation.

State exports are strong cross-stack references; removal requires deliberate
migration. No hotswap/express bypass. UID/GID 10001, read-only root and declared
`/tmp` volume must retain writable permissions in ECS. No shared-data mount or
production test shim is used.

## Company universe

[PRODUCTION_UNIVERSE](PRODUCTION_UNIVERSE.md) records the reviewed initial scope.
Its create-only seed is already applied. Do not rerun it as a readiness check;
later company changes require separately reviewed inputs.

For authorized initial setup, first inspect whether the target table is empty:

```bash
aws dynamodb scan --table-name finbot-prod-companies --select COUNT \
  --consistent-read --limit 1 --no-paginate \
  --profile default --region us-east-1 --output json --no-cli-pager
```

Count one means records exist, not that the whole table has one row. After reviewing
identities, the saved transaction creates all 50 or none:

```bash
aws dynamodb transact-write-items --cli-input-json file://infra/seed/companies.prod.json \
  --profile default --region us-east-1 --output json --no-cli-pager
```

Preserve create-only conditions; do not delete rows to retry. The fixed request
token permits identical retries for ten minutes; an already applied seed later
fails its create conditions. Capacity returned is not a row count. Verify actual
base-table attributes, then the runtime's eventual enabled index:

```bash
aws dynamodb query --table-name finbot-prod-companies --index-name EnabledCompanies \
  --key-condition-expression 'enabled_marker = :enabled' \
  --expression-attribute-values '{":enabled":{"S":"ENABLED"}}' --select COUNT \
  --profile default --region us-east-1 --output json --no-cli-pager
```

Initial scope is 50. Do not request strong GSI consistency; allow propagation and
investigate persistent mismatches rather than reseeding. Runtime configuration is
DynamoDB, not the historical seed/document.

## GitHub application delivery and activation

[infra/github](../infra/github/README.md) owns setup files and exact CLI commands.
`FINBOT_DELIVERY_ENABLED` is repository-scoped; target/activation variables are
production-environment-scoped. `FINBOT_ACTIVATION_APPROVED` permits active delivery;
manual `activate=true` additionally requests startup of a stopped service. Each
production release requires environment approval. Setting flags does not dispatch,
stop or cancel work.

CI runs offline application/CDK/workflow/rebuilt ARM64 checks without AWS credentials.
Delivery follows successful same-repository main-push CI or manual main dispatch,
serializes without cancelling handoffs and obtains OIDC only in the release job.
Stopped environments stay stopped on ordinary delivery. **`activate=false` does
not stop an already active service.** Explicitly stop before incompatible staging.

To activate or roll back to an already validated compatible digest:

```bash
gh workflow run deploy.yml --repo ilancooke/finbot-filings --ref main \
  -f activate=true -f image_digest=sha256:REVIEWED_DIGEST
```

Inspect commit/digest/input and pending deployment before approval; inspect the
retained manifest afterward. Selecting an existing digest avoids rebuilding between
validation and activation. `release.py` validates baseline/roles/platform/target
and digest before interruption. Controlled handoff/restoration uses:

1. Register replacement; retain predecessor revision/digest/count.
2. Scale zero; paginate task listings, repeatedly observe tasks and wait for all
   actual STOPPED states, including outgoing/stopping tasks.
3. Install selected revision at zero. Require its single COMPLETED deployment and
   no non-STOPPED tasks.
4. Separately request count one only when activation is permitted.
5. Verify actual revision/digest/task health and rollout; a generic stability
   waiter alone can accept a rolled-back revision.

Separate revision/count updates prevent the startup scheduling race observed in
filtering activation. APIs are eventual and these checks are not distributed
exclusion. Planned releases incur outage; ECS startup adds 150 seconds before SEC.
Release waits have finite SDK/subprocess bounds, a 1200-second deadline and separate
restoration deadline; workflow timeout is 55 minutes. Forced runner termination
can interrupt restoration. Successful restoration does not turn a failed release
into success.

### Filtering compatibility

Keep the fixed cutoff in CDK/task configuration. Pre-filter images cannot validate
skip checkpoints and are unsuitable active rollback targets. For incompatible
schema/configuration changes: pause delivery, stop/wait, merge/pass CI, stage while
stopped, pause again, preserve staged digest in tracked CDK configuration, review/deploy and update the
baseline variable, then reactivate the reviewed digest. No bulk migration/queue
purge is needed. Published objects/events and terminal facts remain intact.

The `earnings-acquisition-v2` attachment-routing revision uses normal application
delivery; it requires no CDK deployment or schema migration. Pending affirmative
v1 work can upgrade to v2 during re-evaluation. Blocked checkpoints retain their
guards, and completed enumeration snapshots are not reopened automatically.
Recovering previously omitted attachments requires a separate reviewed operation
with explicit filing/attachment scope; do not reset acquisition state or widen
the cutoff as part of this release. V1 images cannot update v2 pending selection
checkpoints; use a rollback image supporting v2 checkpoints. See the
[current rules](ARCHITECTURE.md#filing-admission-and-document-selection).

## Manual infrastructure changes and release recovery

Serialize CDK against delivery. Disable the repository flag and verify no release
is running; the flag does not cancel one. Scale zero with approved tooling, wait
for observed tasks to STOPPED, and preserve current digest in tracked CDK inputs
before diff/deploy. Runtime CDK leaves desired zero. Update saved GitHub variables
from the new `BaselineTaskDefinitionArn`, then reapply them. This is CDK's baseline,
not each application revision. Explicitly reactivate after review; never blindly
reconcile against an old image.

| Failed/cancelled release outcome | Action |
| --- | --- |
| `failed_restored` | Independently verify prior revision/digest/health; workflow remains failed |
| `failed_restore_unverified` / missing checkpoint | Pause, scale zero, prove STOPPED, inspect logs/events; restore a retained compatible digest through the same path |
| `staged` | Zero count is intentional; delivery did not activate |

Do not reset terminal facts/delete raw objects to repair service state.

## One-shot live calendar check

Use the production provider without AWS/SEC/runtime startup or durable writes:

```bash
.venv/bin/python -m finbot_ingestion.calendar.check \
  --companies-file infra/seed/companies.prod.json --days 30 \
  --output /private/tmp/finbot-calendar-check-NEW-ID.json
```

Output must be new; optional `--start-date YYYY-MM-DD` fixes dates. This is a bounded
live Yahoo check; help is offline. It uses a new private temporary cache, inclusive
New York dates and normal bounds (600 seconds, 300 HTTP attempts, 30000 rows) with
validated `YAHOO_*` overrides. For changed scope review a new company input.
Require `status=collection_complete`, correct scope/matches and reviewed absences.
Exit zero proves consistent observed collection, not all-company coverage,
authoritative cancellation, durable sync, Fargate access, ongoing rights or future
reliability. Reports exclude raw payloads/cookies/authenticated URLs/arbitrary
errors. Preserve evidence outside Git; do not invent events/substitute tickers to
force matches. Keyless access does not settle ongoing provider-use arrangements.

## Finite Fargate deployment check

`--deployment-check --check-id ID` dispatches before ingestion wiring, refusing
credentials outside the task role. Run a separate finite diagnostic while normal
ingestion is stopped. It makes bounded AWS test writes but no SEC/Yahoo/company
writes/SNS/SQS work. Budget is 180 seconds; SDK connect/read timeouts are three/five
seconds with two attempts. It validates the 50-company initial scope.

Use [RunTask input](../infra/checks/run-task.prod.json) as a reviewed template.
**Its historical check was executed and cleaned up.** A new execution needs a new
check ID, RunTask clientToken/startedBy and matching
[cleanup input](../infra/checks/delete-checkpoints.prod.json). Verify current
network/region/roles and exact staged task-definition ARN/digest. Release role
cannot launch this standalone task; no application IAM expansion is expected.

| Resource | Owned fixture for ID |
| --- | --- |
| Raw S3 | `deployment-checks/ID/probe.txt`, fewer than 100 bytes, SSE-S3 |
| Calendar | `expected_date=__deployment_check__`, `cik=__deployment_check__:ID` |
| Filings | `accession_number=__deployment_check__:ID` |
| Artifacts | `artifact_id=__deployment_check__:ID` |
| Logs/EMF | Sanitized report; `Finbot/DeploymentChecks` / `DeploymentCheckSucceeded` |

Rows have no application schema/pending keys and lie outside date/identity queries.
Ownership-guarded writes/strong reads reject existing fixtures. Check S3 absence
404, duplicate create 412, unconditional overwrite denial 403 and original
version/bytes. Validate private writable `/tmp`/atomic heartbeat and checkpoint
roundtrips. Load calendar dependencies and touch 256 MiB of buffers, requiring
256 MiB headroom in the initial 1-GiB task. This is synthetic, not production peak
memory; it emits no RuntimeHealthy metric.

```bash
aws ecs run-task --cli-input-json file://REVIEWED_RUN_TASK_INPUT.json \
  --task-definition STAGED_TASK_DEFINITION_ARN \
  --profile default --region us-east-1 --output json --no-cli-pager
```

Require one task/no failures. Wait using `aws ecs wait tasks-stopped`, then describe
expected digest/override and exit zero. Read `ingestion/ingestion/<task-id>` in
`/finbot/finbot-prod`, require passed report and independently extracted
`DeploymentCheckSucceeded=1` with expected Service/Environment. Log receipt alone
is not EMF verification. Confirm stopped service/diagnostic task.

Preserve report/exact S3 version before operator cleanup:

```bash
aws dynamodb transact-write-items --cli-input-json file://REVIEWED_CLEANUP_INPUT.json \
  --profile default --region us-east-1 --no-cli-pager
aws s3api delete-object --bucket REVIEWED_BUCKET \
  --key deployment-checks/ID/probe.txt --version-id REPORTED_TEST_VERSION_ID \
  --profile default --region us-east-1 --no-cli-pager
```

Verify strong row absence/S3 HEAD 404. Partial/lost acknowledgments require inspecting
owned keys first; complete cleanup transaction fails for missing/different owners.
Never use unversioned deletion or touch application objects. A failed-to-start task
can reuse check ID only after proving no fixtures exist, with fresh RunTask token.
An executed/partial check needs new ID and matching cleanup keys/tokens. This does
not test both AZs, live provider egress or sustained memory.

## Readiness and post-release verification

Before a new environment/material image change, review correct resources/scope,
explicit Yahoo/SEC identity, compatible immutable digest/scan findings, task-role
permissions, writable cache/heartbeat, logs/EMF, memory and retained data/images.
Use cloud diagnostics where needed and verify cleanup. Confirm alarms, reviewed
calendar absences and ongoing provider access. Placeholder cannot establish readiness.

After release independently verify one healthy ARM64 task/exact digest/cutoff,
full calendar company/date scope and age, configured provider, SEC progress,
selection diagnostics and selected S3/stored/published facts. HEALTHY alone does
not prove calendar coverage/delivery. Short observations do not establish sustained
reliability or worst-case memory. Record unresolved findings in [BACKLOG](BACKLOG.md).

## Automatic recovery and alarms

[ADR 008](DECISIONS.md#adr-008): active count one, min/max 0/100, stop timeout 120s,
SEC quiet period 150s, ECS health allowance 300s, 30s checks/5s timeout/three retries.
Local Docker uses zero quiet delay/120s allowance. Calendar outages alarm separately
from live process health. Recovery can miss earnings latency goals; measure ARCH-001.

Set ignored `alarm_email` for a retained managed SNS subscription/topic, or
`alarm_action_arn` for an existing same-account/region destination. Enabled
monitoring without destination fails. Managed policy scopes CloudWatch publishing
to exact account/alarm ARNs and HTTPS. Provision with actions disabled, confirm
email/receipt, then review/deploy action enablement. Notification setup does not
start ingestion. Verify SNS receipt with reviewed `aws sns publish`/inbox; MessageId
alone proves only acceptance. For CloudWatch-originated delivery while stopped,
inspect actions and temporarily reset the liveness alarm:

```bash
aws cloudwatch set-alarm-state --alarm-name REVIEWED_LIVENESS_ALARM \
  --state-value OK --state-reason 'Operator notification test; ingestion intentionally stopped.' \
  --profile default --region us-east-1 --no-cli-pager
```

Normal missing-heartbeat evaluation restores ALARM; verify successful SNS action
history/email. This changes temporary alarm state, not configuration/data/ECS count.
The managed alarm has only SNS ALARM action, no compute/scaling action.

Eleven alarms use `Finbot/Ingestion`, Service=`finbot-ingestion`, Environment from
config, plus native failed-work SQS metrics. Liveness treats three missing/unhealthy
one-minute periods as breaching. Calendar scope/stale/provider checks do not require
an age sample. Failed-work counts/age, SEC/publication errors, terminal transitions
and acceptance-to-discovery latency surface issues. Non-liveness sparse missing
data is nonbreaching. Planned stops can breach liveness; evaluation continues stopped.

TerminalFailures emits on new transitions, not continuous zeros. Sparse evaluation
may retain ALARM longer than nominal window; OK does not mean failures repaired or
queue emptied. Historical catch-up can inflate acceptance latency. No default
RunningTaskCount, Container Insights or metrics sidecar is required.

## Failed-work investigation

Correlate `ingestion.work_failed` envelopes, durable terminal/dead-letter facts and
contextual logs. Queue retention is 14 days; checkpoints persist. Normal workers
never redrive. Do not consume/delete evidence or raw bytes as incidental repair.

```bash
aws logs filter-log-events --log-group-name /finbot/finbot-prod \
  --filter-pattern '{ $.message = "Workflow attempt failed" }' \
  --profile default --region us-east-1 \
  --query 'events[].{Task:logStreamName,Event:message}' --output json --no-cli-pager
```

Selection logs use `operation=acquisition_selection`,
`policy_version=earnings-acquisition-v2`, `selection_decision` and stable reason
(historical logs can show v1).
`unresolved_items`, `conflicting_items`, `unresolved_exhibit_role` and
`possible_image_only_content` need review; they are not terminal download failures.
Clear selected documents can proceed with unresolved attachments omitted.

## Last recorded verification

These are dated observations, **not a fresh live inspection**.

### Cutoff and configuration rollout (2026-10-12 UTC)

ADRs 014–015 were deployed via [run 38199947638](https://github.com/ilancooke/finbot-filings/actions/runs/38199947638).
CDK/GitHub baseline is revision 16; active revision 17 was verified healthy with
cutoff `2026-10-06T04:00:00Z`, matching image, non-stale calendar and resumed SEC
polling. Automatic delivery is enabled. No acquisition reset was performed.

### Readable-layout reset (2026-10-11 UTC)

The readable-layout
reset completed **2026-10-11 UTC (October 11 locally)** under ADR 013.

| Evidence | Recorded result |
| --- | --- |
| Application validation | [CI 38154317521](https://github.com/ilancooke/finbot-filings/actions/runs/38154317521) passed; local application 648 passed/8 offline-gated skips, CDK 29 passed, ARM64 container 8 passed; template lint had no errors and guard passed |
| New image | `sha256:b586350bfa6012b050c13066c7deba7a8550ad58da86342f9672b68f52a5ee2a`; ARM64; scan retained the previously accepted zlib finding |
| Staged infrastructure | CDK baseline 12, compatible image and `2026-10-09T04:00:00Z` cutoff; runtime staged at zero before deletion |
| Activation | [Run 38155193152](https://github.com/ilancooke/finbot-filings/actions/runs/38155193152) succeeded from `111f1a3635a849e0242ddcca6bd283ff3e78bbf8`; application revision 13, service `1/1/0`, COMPLETED/HEALTHY, exact digest/cutoff verified |
| Reset inventory | 112,433 S3 object versions/delete markers, 3,120 Filings rows, 113,204 Artifacts rows, zero Calendar satisfaction rows; 14 historical failed-work messages |
| Empty-state verification | All S3 versions/delete markers, Filings/Artifacts rows, Calendar satisfaction rows and visible/in-flight/delayed failed-work messages verified zero before restart |
| Preserved configuration | Exact strong-read comparison retained all 50 Companies and 48 non-satisfaction Calendar records |
| Failed-work alarms | Both Visible and Age alarms returned OK at `16:25:53Z` and `16:25:36Z`, respectively |
| Runtime | Matching/non-stale/configured calendar, zero startup queues/recovery errors; SEC polling resumed after the normal quiet period |
| Fresh acquisition | HOVR's October 9 10-Q (`0001213900-26-108211`) and earnings 8-K (`0001213900-26-108213`) rediscovered at `16:30:10Z`; three selected HTML artifacts stored/published, zero retries/pending/terminal facts; parent enumerated child counts 1/2 |
| Live object verification | Three `HOVR/2026-10-09_{10-Q,8-K}_<accession>/<filename>` objects; HEAD sizes 487,327/32,065/21,073 bytes agree with database, SSE-S3 and version 2 acceptance provenance verified; matching new S3 URIs and publication checkpoints |

Reset inventory and verification remain outside Git under
`/private/tmp/finbot-acquisition-reset-20261011-readable{,.verified}.json`, with
preserved-configuration evidence alongside. An initial partial S3 deletion stopped
on per-object errors; resumption used the original reviewed inventory and bounded
transient-error retries. No database deletion occurred in the failed attempt.
Historical terminal records and queue messages were deliberately removed under
the reset scope; they were not repaired/redriven. Older logs and the dated evidence
below remain historical.

Fresh acquisition evidence is `/private/tmp/finbot-layout-live-evidence.json`.
Storage and acknowledged SNS publication were verified; downstream receipt was
not tested. First safety polls remain staggered across an hour, so additional
companies repopulate through the normal schedule.

Automatic GitHub delivery was restored after the reset. Pre-reset images remain
unsuitable active rollback targets for this layout.

### Historical filtering verification

These are dated observations, **not a fresh live inspection**. Migration/deployment
and filtering activation are complete. Last checkpoint: **2026-10-11 UTC
(October 10 locally)**.

| Evidence | Recorded result |
| --- | --- |
| Filtering activation | [Run 38102021956](https://github.com/ilancooke/finbot-filings/actions/runs/38102021956), completed `2026-10-11T01:36:57Z`; release-script commit `e07bd6fe17c43d2b8538d826bd9d2208d8ae629f` |
| Application | Filtering source `182f456bf5586389dc925e4b1289ba3d3534df2c`; digest `sha256:b98313062d1b5edff55f3cd3fa19ea43ab62f9bb07dc8e655ba47e0b9d735c3d` |
| Service | Application revision 10, CDK/GitHub baseline 8, `1/1/0`, COMPLETED/HEALTHY ARM64, fixed cutoff |
| Recovery | 759 before-cutoff skips/no warnings/errors; observed pending ENUMERATE/ACQUIRE/PUBLISH counts zero; strong sample skips/four original terminal bundles preserved |
| Runtime | Matching/non-stale/configured calendar, empty startup queues; no newly eligible filing in inspected startup window |
| Local Yahoo | October 9–November 7: 50 companies/49 observations; NVDA absence reviewed as expected |
| Initial cloud Yahoo | October 10–November 8: 50 companies/47 events, completed `2026-10-10T14:32:05.877115Z` |
| Actual acquisition | Citigroup `0000831001-26-000045/R1.htm`: 73,390-byte SSE-S3 object/stored/published facts; downstream receipt untested |
| Finite diagnostic | Revision 4 passed/exit zero; 50 reads/three checkpoints/immutable test bytes/EMF; peak RSS 366 MiB with 256-MiB buffers; fixtures later verified absent |
| Notifications | Subscription/email and CloudWatch liveness SNS path verified October 10; not every threshold tested |

Initial image pull failed once before Python startup; same digest succeeded on
retry without image/network/IAM changes, cause undetermined. Filtering activation
initially failed/restored stopped state due to combined revision/count startup.
Separate updates succeeded using the same application digest; release scripts run
outside the application image from workflow checkout. Detailed intermediate
approvals/tasks/CI counts remain in Git.

At that checkpoint, four oversized complete-submission TXT artifacts retained
original terminal facts; selection did not repair/delete them. The later ADR 013
reset cleared acquisition state explicitly. HIGH `CVE-2026-85091` in initial Debian trixie
zlib was operator-accepted for research, not suppressed/fixed. Recheck on image
updates. Open selection/provider/memory observations are in [BACKLOG](BACKLOG.md).

Recorded production references: account `559007813222`, region `us-east-1`,
`finbot-prod-state`/`finbot-prod-runtime`, cluster/service `finbot-prod`, family
`finbot-prod-ingestion`, log group `/finbot/finbot-prod`, raw bucket
`finbot-prod-state-artifacts82dd59a1-8gt63rcgmaua`, tables
`finbot-prod-{companies,calendar,filings,artifacts}`, artifact SNS
`arn:aws:sns:us-east-1:559007813222:finbot-prod-artifact-ready`.
[Saved GitHub variables](../infra/github/production-variables.env) retain reviewed
targets. Obtain current stack outputs before operations; historical references
are not authorization.

## Targeted DAL/WFC/JNJ attachment recovery

`scripts/recover_earnings_attachments.py` is a local operator command for these
four v1 omissions, not a general backfill or runtime job:

| Ticker | Completed 8-K accession | Missing attachment | Expected content |
| --- | --- | --- | --- |
| DAL | `0000027904-26-000035` | `deltaairlinesannouncessept.htm` (EX-99.1) | Quarterly earnings release |
| WFC | `0000072971-26-000318` | `wfc3qer10-13x26ex991xrelea.htm` (EX-99.1) | Quarterly earnings release |
| WFC | `0000072971-26-000318` | `wfc3qer10-13x26ex992xsuppl.htm` (EX-99.2) | Quarterly financial supplement |
| JNJ | `0000200406-26-000189` | `a2026q3exhibit992.htm` (EX-99.2) | Supplementary sales data, earnings statements and reconciliation |

Run from the repository root using its installed `.venv`. The helper reads the
account and SEC contact header from ignored `infra/cdk/config.local.json`
(`--private-config` overrides the path), and uses the selected AWS profile.
It checks the fixed production account/service/tables, pins the current task
definition/image in the manifest and respects its configured cutoff/SEC rate.
The application must already be running a v2-compatible image before this
operation; the helper does not build or deploy an image. No CDK deployment,
schema migration or infrastructure change is required for the helper.

1. Pause delivery with `FINBOT_DELIVERY_ENABLED=false`; verify no release is
   in progress. Capture current service/task identifiers, scale desired count
   to zero, wait for the captured tasks to stop, and verify zero desired,
   running and pending tasks. Both helper modes require all observed service
   tasks to be STOPPED before making SEC requests. Keep delivery paused until
   recovery and restart verification finish.
2. Create a new preview file outside Git. This fetches the three package indexes,
   directory snapshots and required primary routing bytes; it makes no AWS writes
   and does not download the four attachments:

   ```bash
   .venv/bin/python scripts/recover_earnings_attachments.py --profile default \
     --manifest /private/tmp/finbot-earnings-recovery-NEW-ID.json
   ```

   Review the four `additions`, selection reasons, canonical parents and pinned
   task/image. The manifest also records all package metadata/decisions and hashes
   fetched primary routing bytes. Preview refuses preexisting targets, blocked or
   incompatible parents, and any additional selected-but-missing attachment.
3. Execute the reviewed preview while still stopped:

   ```bash
   .venv/bin/python scripts/recover_earnings_attachments.py --profile default \
     --manifest /private/tmp/finbot-earnings-recovery-NEW-ID.json --execute
   ```

   Execution re-fetches SEC evidence and strong canonical records, rejects changes
   before writing, and conditionally creates only the four canonical child records
   with v2 SELECT checkpoints and pending ACQUIRE eligibility. It verifies those
   checkpoints, preserved parents and existing selected children before reporting
   `queued`. The first enumeration timestamp/count remains unchanged; those counts
   describe the original snapshot, not today's number of children. No S3/SNS/SQS,
   calendar, satisfaction or company writes occur in the helper. If interrupted,
   keep ingestion stopped and resume **the same manifest** with `--execute`;
   compatible partial creates/selection writes are idempotent. Unexpected conflicts
   stop the operation for review. Do not delete records or regenerate the preview
   to bypass a conflict.
4. Restore desired count one on the same service/task definition and wait for
   stability. Normal pending recovery downloads/stores the four raw attachments,
   publishes their artifact-ready events, and performs normal calendar satisfaction
   handling. Verify HEALTHY task/container state, four strong artifact records with
   `stored_at`, `s3_uri` and `published_at`, corresponding S3 keys, and no new terminal
   failures. Existing files/events are not replayed. Then restore delivery with
   `FINBOT_DELIVERY_ENABLED=true`. Keep the preview outside Git for interrupted
   recovery.

## Explicit acquisition reset

ADR 013 authorizes the October 2026 layout transition. Repeating a destructive
reset requires a new explicit operator scope. Validate the application/CDK tests,
ARM64 image, offline container lifecycle and template diff first. Pause GitHub
delivery, verify no release is running, stop/wait for actual STOPPED tasks, then
stage the compatible image and cutoff with CDK at desired zero. Update the saved
GitHub baseline ARN. Do not activate an earlier image against the new layout.

The reset helper is intentionally scoped to the recorded production account,
region, raw bucket, service and tables. Its default command is read-only:

```bash
.venv/bin/python scripts/reset_acquisition.py --profile default \
  --manifest /private/tmp/finbot-acquisition-reset-NEW-ID.json
```

Review counts and exact keys/versions before the explicit execution:

```bash
.venv/bin/python scripts/reset_acquisition.py --profile default \
  --manifest /private/tmp/finbot-acquisition-reset-NEW-ID.json --execute
```

It requires zero desired/running/pending tasks and all observed tasks STOPPED.
It deletes version IDs and delete markers, Filings/Artifacts rows, only Calendar's
`__event_satisfaction__` partition, and purges failed-work SQS. Companies and all
other calendar records remain. It checks the account/staged task definition,
refuses unplanned inventory additions, checks per-object errors, retries unprocessed
DynamoDB writes, waits for queue purge and verifies empty reset targets before exit.
The adjacent `.verified.json` records successful verification. It never starts
ingestion or changes infrastructure/IAM. If interrupted, keep stopped, inspect
remaining inventory and resume from the same manifest (or review a new inventory).
Do not blindly retry an SQS purge still in progress.

Only after verified empty state, activate the compatible digest via the normal
release path. Verify the fixed cutoff, readable keys, original bytes/provenance,
durable selected/stored/published checkpoints, calendar health and queue alarms.
Deletion is permanent and downloads incur fresh provider/AWS request activity.
Preserve inventory/results outside Git; log history and older dated deployment
evidence remain historical, even though their referenced data was reset.
