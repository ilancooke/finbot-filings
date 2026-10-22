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
| State stack | Raw S3, four tables/indexes, Filings SNS, failed-work SQS, application ECR |
| Runtime stack | Network, cluster/service, baseline task definition, roles, logs and alarms |
| GitHub release | Digest-pinned application revisions matching baseline configuration except image |
| Operator | Reviewed configuration, company inputs, initial readiness and infrastructure maintenance |

Task role accesses application data; execution role pulls images/writes logs;
release role publishes images/updates this service. Release cannot deploy CDK,
mutate IAM/data or launch standalone tasks. Bootstrap assets are separate from
application artifact storage/ECR. Baseline and application revisions have different
owners; do not change the baseline variable to each GitHub-created revision.

## FilingReady rollout

ADRs 019–020 replace per-document events with `filing.ready` / `1.0` on the new
`finbot-prod-filing-ready` SNS topic. This release changes application configuration
and state/runtime infrastructure. Use the following order; do not deploy the new
application to an active old baseline or activate the old application with the new
baseline's `FILING_READY_TOPIC_ARN` setting.

1. Pause delivery (`FINBOT_DELIVERY_ENABLED=false`), verify no release is running,
   and update/restart the dashboard with the reserved `__filing_ready__/` record
   filter. Stop ingestion and wait for every observed service task to be STOPPED.
2. Commit/review the changes and require successful main CI, including rebuilt
   ARM64 lifecycle checks. While ingestion remains stopped, enable delivery and
   manually stage with `activate=false`. Inspect the reviewed run and record its
   new image digest; disable delivery again after staging. A stopped task can
   carry the old baseline temporarily, but must not be started with it.
3. Save that compatible image digest in `infra/cdk/config.prod.json`. Select both
   public/private inputs as described below. Synthesize and review both stack
   diffs. The state change should add `FilingReady` and its HTTPS-only policy;
   existing resources and exports, including the old topic/export, stay intact.
   The runtime changes the task SNS publish grant and environment to the new topic
   and stays at desired count zero.
4. Deploy the reviewed state stack first, then the runtime stack, using the manual
   CDK procedures. Read the new state `FilingTopicArn` output and verify the stopped
   baseline uses it in `FILING_READY_TOPIC_ARN` with a publish grant only to that
   topic. Update the saved GitHub `BaselineTaskDefinitionArn` variable from the new
   runtime output and reapply the environment variables.
5. Restore delivery and explicitly activate the reviewed image through the normal
   application workflow. Verify actual revision/digest, new topic ARN, task health,
   calendar state and SEC progress. Verify a new selected filing has one complete
   stored manifest, saved event/ready time and publication checkpoints. Repeated
   delivery retains the same logical payload and snapshot identity.

`ARTIFACT_READY_TOPIC_ARN` is no longer an accepted application setting. Consumers
must subscribe to `FilingTopicArn` and deduplicate by `(filing_id, snapshot_id)`.
Subscriptions on the old topic do not transfer to the new topic. Confirm existing
subscriptions before retiring anything; earnings should bind to the new topic.

The old topic retains its `ArtifactReady` construct/logical ID, physical name,
HTTPS policy and `ArtifactTopicArn` state output. The runtime's
`LegacyArtifactTopicArn` output deliberately preserves its strong import/export so
state can deploy first without an export-in-use failure. It grants no legacy
publish permission and is not an application setting. Retirement of the old topic,
subscriptions and cross-stack reference requires a separately reviewed migration;
this release does not delete them. Do not change/remove the legacy export during
this cutover. See ADR 020.

File publication checkpoints mean inclusion in an acknowledged filing event.
Keep a rollback image compatible with filing PUBLISH work, new checkpoints and
`FILING_READY_TOPIC_ARN`; do not restore an older image blindly. No data reset,
cutoff adjustment, company reseed or historical notification replay is needed.
See [FILING_READY](FILING_READY.md) for exact event contracts and limits.

## Supporting image recovery

The supporting-images v1 release keeps filing admission and FilingReady 1.0
unchanged, but image-bearing snapshots require compatible consumers. This
cloud operation was completed on 2026-10-21; local review-bundle replacement is
tracked separately. The helper is intentionally scoped to ASML
`0001628280-26-065813`, BAC `0000070858-26-000481`, UNH
`0000731766-26-000255`, and CMC `0000022444-26-000065`.

1. Review/commit the changes in finbot-filings, finbot-earnings and
   finbot-filing-evals. Pass each repository's checks. Rebuild ingestion and run
   its offline container lifecycle suite. Build the earnings Lambda asset with
   its pinned Linux Pillow wheel. Deploy/update compatible downstream consumers
   before allowing image-bearing events; preserve the current disabled queue
   setting unless separately activating that service.
2. Pause automatic ingestion delivery (`FINBOT_DELIVERY_ENABLED=false`) and
   any active earnings consumption. Stop ingestion and wait for every service
   task to reach STOPPED. Record the current task definition/digest. Stage the
   compatible ingestion release at desired count zero using the normal delivery
   procedure; `activate=false` preserves zero only if the service is already
   stopped. Do not change the SEC cutoff, reset tables or delete raw objects.
3. From this repository, prepare a new read-only preview. The private config
   supplies account/contact; normal profile credentials stay outside output:

   ```bash
   .venv/bin/python scripts/recover_supporting_images.py --profile default --manifest /private/tmp/finbot-supporting-images-review.json
   ```

   This verifies the account, stopped service and pinned task, reads strong
   canonical records and fresh SEC package metadata/routing evidence, and reads
   original HTML from its canonical S3 objects. It downloads no image files and
   performs no cloud writes. Inspect `image_count`, every proposed image ID,
   `stored_html_sha256`, original membership, and original snapshot state in the
   manifest. Expect 12 ASML, 19 BAC, 16 UNH page images plus CMC content graphics;
   the actual fresh source-backed preview is authoritative. Existing image rows,
   unrelated document omissions, pending unpublished snapshots or conflicting
   state are refused rather than adopted. Preserve this exact manifest.
   Legacy completed ASML/BAC/UNH parents may lack explicit selected membership.
   The helper reconstructs it in the preview only when the current document
   selection matches the original enumeration count and every original child is
   durably stored and published. Execution commits that verified selection plus
   images while preserving the first enumeration facts; preview makes no repairs.
4. When the preview is approved and the service is still stopped, apply that
   same manifest:

   ```bash
   .venv/bin/python scripts/recover_supporting_images.py --profile default --manifest /private/tmp/finbot-supporting-images-review.json --execute
   ```

   The helper revalidates source hashes, directory membership, canonical rows and
   deployment before writing. It conditionally creates only the reviewed image
   children/SELECT checkpoints, then commits complete filing membership before
   restart. Original HTML, prior immutable event rows and first enumeration
   counts/times are unchanged. Interrupted execution can resume with the same
   manifest while stopped; do not delete partial records or regenerate a plan
   to bypass an error. If any verification fails, keep ingestion stopped.
5. Confirm the returned queued image IDs match the reviewed manifest. Restore
   delivery and explicitly activate the compatible digest. Normal ingestion
   acquires the images through the shared SEC limiter and immutable storage
   path. Verify all required images are stored, new complete snapshot IDs are
   published, prior snapshots survive, and no image is terminal/dead-lettered.
   A failed required image must block that filing's new readiness notification.
6. Update the local golden bundle in a separately staged copy from those new
   stored snapshots, including image bytes, source manifests/document inventory
   and integrity. Preserve human label edits/notes and the original bundle;
   do not append loose images, hand-edit snapshot hashes or reuse old integrity.
   This cloud helper intentionally does not migrate a golden bundle. Validate
   the refreshed copy with the review package before replacing the canonical
   bundle, rebuild the catalog, restart Streamlit and inspect ASML/BAC/UNH page
   tables. Until that refresh, the old local bundle will still lack images.

Do not roll back to an image-unaware consumer once image-bearing snapshots are
published. The earnings adapter uses separate image count/byte limits, a new
prompt/config version, and review-required visual evidence; collecting images
does not certify extracted values or automatically confirm human labels.

Recovery verification (2026-10-21): revision 31 acquired all 50 reviewed JPEGs
(ASML 12, BAC 19, UNH 16, CMC 3). Every S3 object matched its stored size, content
type and source provenance. Four complete snapshots were published, containing
15/21/18/4 artifacts respectively. All eight original document records and first
enumeration facts were unchanged; CMC's previous published snapshot was retained.
Automatic application delivery was restored afterward. The stopped-service helper
was updated locally to support the three legacy parents without explicit membership,
using original enumeration counts and stored/published child checks.

Local validation (2026-10-21): 933 package tests passed, nine opt-in container
tests skipped in the normal suite and all nine passed explicitly against the
rebuilt ARM64 image. Wheel, compileall, dependency and whitespace checks passed.
Recovery tests exercised preview invariance, changed-source/task/manifest refusal,
lost-create-acknowledgment resume, preserved first enumeration facts and normal
worker publication without repeating stored-source downloads. Read-only scanning
of the current local bundle selected 12/19/16 ASML/BAC/UNH page images and three
CMC content graphics. This is not a fresh SEC/S3 recovery preview. No recovery,
deployment, paid inference, shared-data write or cloud resource mutation ran.

## SEC request metric cost reduction

ADR 024 uses normal application delivery with no CDK, alarm or environment changes.
Rebuild and validate the image before delivery. After activation, verify new
`SecRequests` EMF records contain only Service/Environment and optional CallType
dimensions, and `Event=sec_request_breakdown` ticker/type logs contain no `_aws`.
Verify the four call-type counts and detail-log counts each sum to the total over
the same completed flush interval. Use the [README queries](../README.md#sec-request-breakdowns-in-cloudwatch)
to avoid summing duplicate views.

Old ticker metrics can remain listed after deployment; inspect timestamps rather
than treating their presence as continued billing. They must receive no new points
from the new runtime. Charges stop in hours without emissions; confirm the decline
in Cost Explorer after billing catches up. Reverting to an older image restores
ticker metric emission and cost. Historical metrics and log records need no deletion.

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

## Earnings-only 6-K rollout

ADR 018 uses normal application delivery with no CDK, IAM, table/index or event
schema changes. Rebuild/test the ARM64 image. Update/restart `finbot-dashboard`
with its v3 matching reader before ingestion delivery. Keep the fixed cutoff;
normal recent-submissions discovery can acquire newly supported 6-Ks on/after it.
Do not reset data or traverse history as part of this release.

New selection writes use `earnings-acquisition-v3`; domestic routing is unchanged.
Affirmative v1/v2 pending work may upgrade. Existing exclusions/unresolved work,
completed enumeration timestamps/counts, bytes, publications and terminal facts
remain guarded. An original 6-K becomes calendar-match eligible only after its
completed `SELECT` / `earnings_6_k` parent checkpoint. The next normal poll can
create an `earnings-satisfaction-v3` match using the existing New York day bounds,
without document redownload/republication. Amendments do not satisfy expectations.

Verify selected release/statement artifacts and minimal parent/child checkpoints,
no acquisitions for known meeting/future-announcement filings, visible unresolved
routing, and dashboard calendar links after a poll/Refresh. This is a conservative
selector; opaque labels/images can remain uncovered. Keep v1/v2/v3 matching and
6-K form support in any rollback image; do not delete state to run an older image.

The four-attachment operator helper also accepts the reviewed v3 selector because
8-K routing is unchanged. Its fixed targets/stopped-service checks remain. Its
manifest pins the policy and task/image: an existing v2 manifest must be resumed
with its compatible original helper/image, not rewritten or bypassed with a new
preview after partial execution.

### October 15 ERIC/TSM unresolved-parent recovery

ADR 023 uses normal application delivery, with no infrastructure/schema changes.
Merging/deploying the selector does not reopen existing UNRESOLVED parents.
The separate operator helper is scoped to ERIC `0001628280-26-065962` and TSM
`0001046179-26-000726`. Expected membership is the ERIC cover
`a2610156-kq326.htm` plus `a2026q3forfiling.htm`, and the TSM cover
`tsm-20261015x6k.htm` plus `a3q26e_withguidancexfinal.htm`. TSM's presentation is
excluded. The other six reviewed non-earnings holds are outside this operation.

Deploy the tested image first. For explicitly authorized recovery, pause delivery,
stop ingestion and prove every observed task STOPPED using the existing handoff
procedure. Keep that fixed task definition/image installed while generating and
executing the preview. From the repo root, using the matching reviewed checkout:

```bash
# Read-only AWS/SEC inspection; creates a NEW local manifest:
.venv/bin/python -m scripts.recover_unresolved_6k --profile default \
  --manifest /private/tmp/finbot-eric-tsm-recovery-NEW.json
# After reviewing the two parents, exact filenames, fingerprints and changes:
.venv/bin/python -m scripts.recover_unresolved_6k --profile default \
  --manifest /private/tmp/finbot-eric-tsm-recovery-NEW.json --execute
```

The helper verifies account/region/table/task/image/cutoff, current unresolved
identities, no completed/terminal/publication facts or observed children, and normal
selector decisions. Preview makes no AWS writes. Execution revalidates the entire
saved plan/source, then uses revision-conditional parent updates to SELECT and
ENUMERATE, preserving original identities/timestamps/failure history. It never
deletes rows, writes S3, creates children or publishes events itself. Strong reads
also check the two expected artifact IDs; index reads reject other observed children.
The service must remain stopped throughout; these checks are not distributed fencing.

On interruption/lost acknowledgment, keep ingestion stopped and rerun execution
with the same manifest, checkout and task/image. Already committed exact parent
transitions are no-ops; changed source/state or unrelated work is rejected. Do not
generate a new preview after partial execution. After verified execution, restart
the tested image through the normal activation path. Normal enumeration/acquisition
must store four artifacts total and publish two complete FilingReady events. Verify
original bytes, exact selected membership, stored/published checkpoints, no new
terminal failures, and the two dashboard entries after Refresh. Re-running the
operator helper after processing completes is intentionally rejected.

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
| `discovery_latency_alarm_enabled` | Default false; explicitly opt in when latency becomes an operational commitment |
| `discovery_latency_ms`, `failed_work_age_seconds` | Initially 60000 / 300; latency threshold applies only when its alarm is enabled |

Read the actual GitHub OIDC subject before configuring trust:

```bash
gh api repos/ilancooke/finbot-filings/actions/oidc/customization/sub
```

Append `:environment:production` to the returned prefix, including permanent IDs
when immutable subjects are enabled. `use_default=true` does not imply name-only
subjects. Recorded production subject:
`repo:ilancooke@8453151/finbot-filings@1341430877:environment:production`.
Trust also requires `aud=sts.amazonaws.com`. Protect production with the main-only
environment branch policy. Routine delivery has no required reviewer or wait timer
(ADR 025). No static AWS keys belong in variables/images.

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

[PRODUCTION_UNIVERSE](PRODUCTION_UNIVERSE.md) records the reviewed scope.
`infra/seed/companies.prod.json` is the consolidated input for all 72 reviewed
companies. Append newly reviewed company records to this file, then run:

```bash
.venv/bin/python scripts/load_companies.py --profile default
```

This works with an empty or populated table: missing CIKs are conditionally
inserted; existing CIKs are skipped successfully. It never overwrites existing
names/symbols, re-enables disabled companies or removes records absent from the
file. The runtime reads DynamoDB, not the file. Review ticker/CIK/name identities
against SEC before appending; the loader does not resolve them or call providers.

For a read-only preview, append `--dry-run`. `--companies-file PATH` selects a
reviewed alternate input; all rows must target one explicit table ARN. The loader
uses its region and checks the active AWS account against its account before
accessing DynamoDB. Use normal operator credentials with STS GetCallerIdentity,
DynamoDB GetItem and conditional PutItem access to that table. No ingestion-role
or infrastructure permission changes are needed.

The complete input is validated before opening clients; identical duplicate CIK
records collapse, and conflicting duplicates or ambiguous ticker mappings reject
the input. Each CIK is strongly read first. Conditional creation handles concurrent
writers/lost acknowledgments by strongly rereading and reporting a skip. Existing
record differences are listed by field name in `existing_differences`; this command
is additive and does not reconcile changes to existing configuration.

JSON output includes `status`, `requested`, `inserted`, `skipped`, `would_insert`,
`duplicates_in_file` and `existing_differences`. Existing rows are a successful
outcome. Malformed inputs, wrong-account credentials and actual AWS failures exit
nonzero; failures retain completed progress counts without exposing raw SDK errors.
Loads are per record, not an all-or-nothing transaction. If interrupted or failed,
resolve the underlying error and rerun the same command: earlier inserts are now
skipped. There is no transaction-token window or 100-record transaction limit in
the loader. No service restart or image rebuild is required for this local tool.

The underlying file retains its create-only DynamoDB transaction format, but do
not submit the full file directly to `transact-write-items` for routine additions;
that API still rejects the whole transaction if any row already exists. The loader
ignores its top-level token and processes records individually. Do not delete rows
or remove the conditional protection to make a load succeed.

For an independent enabled-universe count (GSI propagation may lag):

```bash
aws dynamodb query --table-name finbot-prod-companies --index-name EnabledCompanies \
  --key-condition-expression 'enabled_marker = :enabled' \
  --expression-attribute-values '{":enabled":{"S":"ENABLED"}}' --select COUNT \
  --profile default --region us-east-1 --output json --no-cli-pager
```

The historical initial scope was 50; after the October 15 additions the reviewed
current scope is 72. Do not request strong GSI consistency; allow propagation and
investigate persistent mismatches rather than reseeding. Runtime configuration is
DynamoDB, not the local seed/document.

### October 15 universe expansion

A separate create-only transaction was applied on October 15 locally (October 16
UTC), adding 22 SEC-verified companies; META was already enabled. Strong reads
verified exactly 72 enabled records and preserved all 50 original records. Those
inputs are now combined in
[`companies.prod.json`](../infra/seed/companies.prod.json); the separate additions
file has been removed. This local consolidation made no AWS changes. Do not
submit the combined raw transaction for verification; use the additive loader
for repeat loads.

The enabled index returned all 72 CIKs and the running service attempted a Yahoo
refresh with all 72 at `2026-10-16T00:23:53Z`, without a restart. Calendar refresh
failed with `IncompleteCalendarSnapshot`, also observed before the update. Last
successful calendar collection was October 13; fresh expanded calendar coverage
was not established. Safety polling remains enabled.

Company changes enter the running scheduler on its normal five-minute reload;
no application deployment or restart is needed. Additions without calendar hints
use safety polling until the next scheduled daily Yahoo refresh supplies their
observations. The active refresh loop does not immediately advance its deadline
when company scope changes. Keep the fixed live cutoff for all additions.
See [the reviewed identities and consolidated diagnostic input](PRODUCTION_UNIVERSE.md#october-15-2026-additions).
The finite cloud deployment diagnostic remains pinned to its original 50-company
scope; update that reviewed expectation before running it against the expanded
universe. No finite diagnostic was run for this data-only expansion.

## GitHub application delivery and activation

[infra/github](../infra/github/README.md) owns setup files and exact CLI commands.
`FINBOT_DELIVERY_ENABLED` is repository-scoped; target/activation variables are
production-environment-scoped. `FINBOT_ACTIVATION_APPROVED` permits active delivery;
manual `activate=true` additionally requests startup of a stopped service. Routine
delivery has no environment-review approval. The saved environment configuration
must be applied once to remove the previous reviewer requirement; committing it
does not change live GitHub settings. Setting flags does not dispatch, stop or
cancel work.

For normal application changes, commit/push directly to `main`. Leave delivery
enabled. `.github/workflows/ci.yml` owns the whole sequence: its credential-free
validation job runs application/CDK/workflow checks and builds/tests one ARM64
image. Only a successful main push or manual main dispatch can enter its production
release job. Pull requests, when used, validate only; no branch/PR workflow is
required. The tested image is transferred within the same run/attempt, its Docker
image ID is checked, and that image is tagged/pushed to immutable ECR storage and
deployed by resolved digest. Deployment never rebuilds it.

The release job serializes without cancelling active handoffs and obtains OIDC
only in the production environment. `scripts/release.py --github` owns image
publication and ECS handoff/restoration; there is no second delivery wrapper or
workflow. Before publication and again immediately before the first ECS mutation,
it reads current `main` through GitHub. A superseded commit records `superseded`
and leaves the service alone. Lookup errors fail before interruption. If main
advances after handoff starts, the in-progress release finishes or restores; the
newer successful run follows. GitHub may replace pending release jobs, so each
queued job checks the actual branch head rather than relying on queue order.

Stopped environments stay stopped on ordinary delivery. **`activate=false` does
not stop an already active service.** Explicitly stop before incompatible staging.
The repository pause flag is an emergency/maintenance switch, not a daily release
step. It prevents future eligible release jobs; verify existing jobs have finished
before any manual maintenance. Failed validation leaves the running service alone.

To retry the current main commit without a new push, dispatch `ci.yml` with defaults.
Retrying only a failed release job reuses its validation job's image artifact
(retained one day); dispatch a fresh run if that artifact has expired.
To explicitly activate or roll back to an already validated compatible digest:

```bash
gh workflow run ci.yml --repo ilancooke/finbot-filings --ref main \
  -f activate=true -f image_digest=sha256:REVIEWED_DIGEST
```

Review commit/digest/input before dispatch; inspect the retained manifest afterward.
Manual dispatch runs the same validation gate. An existing digest selects the
previously validated image for deployment; it is pulled/checked as ARM64 rather
than replaced by the image built for current-main validation. Dispatch from current
`main` with the compatible prior digest; do not rerun a superseded commit to roll
back. Leave `activate=false` to preserve an intentionally paused service.
`release.py` validates baseline/roles/platform/target
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
| `superseded` | Main advanced before handoff; no ECS mutation occurred; inspect the newer run |

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

Ten default alarms use `Finbot/Ingestion`, Service=`finbot-ingestion`, Environment from
config, plus native failed-work SQS metrics. Liveness treats three missing/unhealthy
one-minute periods as breaching. Calendar scope/stale/provider checks do not require
an age sample. Failed-work counts/age, SEC/publication errors, terminal transitions
surface issues. Acceptance-to-discovery latency remains a metric; its alarm is
opt-in through `discovery_latency_alarm_enabled=true`. Non-liveness sparse missing
data is nonbreaching. Planned stops can breach liveness; evaluation continues stopped.

TerminalFailures emits on new transitions, not continuous zeros. Sparse evaluation
may retain ALARM longer than nominal window; OK does not mean failures repaired or
queue emptied. Historical catch-up can inflate acceptance latency. No default
RunningTaskCount, Container Insights or metrics sidecar is required.

### Discovery latency in research deployments

Keep `discovery_latency_alarm_enabled=false` in tracked production configuration
until discovery latency becomes an operational commitment. This omits the alarm
entirely: no email actions and no ALARM entry in CloudWatch. Disabling actions
alone does not meet that requirement. `DiscoveryLatencyMs` emission and historical
metric data are unchanged; `monitoring_enabled` continues to govern the other
alarms' notification actions. Re-enabling the latency alarm requires an explicit
configuration change and reviewed CDK deployment.

On `2026-10-16T01:13:55Z` (October 15 Pacific), the operator-requested discovery
latency alarm was deleted directly and verified absent; the metric retained data
and the two calendar alarms were unchanged. This scoped operation did not deploy
CDK, change ECS desired count, restart ingestion, or alter company/calendar data.
The deployed CloudFormation stack still records the removed alarm until its next
normal infrastructure reconciliation. Use the updated configuration on that
reviewed deployment so the omitted resource is removed from the stack rather than
recreated. Application delivery does not deploy this CDK change. Alarm configuration
backup and removal verification are saved outside Git in
`/private/tmp/finbot-discovery-latency-alarm-{before,removed}-20261016.json`.

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
`policy_version=earnings-acquisition-v3`, `selection_decision` and stable reason
(historical logs can show v1/v2).
`unresolved_items`, `conflicting_items`, `unresolved_exhibit_role` and
`possible_image_only_content` need review; they are not terminal download failures.
Clear selected documents can proceed with unresolved attachments omitted.

## Calendar alarm investigation (2026-10-16 UTC)

The last successful full Yahoo refresh completed `2026-10-13T14:48:45.962504Z`
for 50 companies, with 45 observed events and date coverage through November 11.
CloudWatch shows repeated `IncompleteCalendarSnapshot` refresh failures from
`2026-10-14T14:48:50Z`, before the October 15 universe expansion. Failed collection
preserves the prior expectations and successful freshness checkpoint.

`CalendarStale` transitioned to ALARM at `2026-10-15T14:49:44Z`, after the last
success reached the configured 172800-second (48-hour) age limit. This is calendar
collection age, not expiry of the stored events' dates. `CalendarScope` has one
recorded transition in the inspected four-day history: OK to ALARM at
`2026-10-16T00:28:04Z`, after the expansion to 72. The current successful snapshot
still covers only 50 CIKs, so its company scope no longer matches. The underlying
refresh failure predates the expansion; the current scope mismatch follows it.

A read-only local reproduction with the production Yahoo adapter rejected the
October 15 first page: requested size 100, reported total 192, but 101 event rows
returned. Follow-up inspection resolved the apparent inconsistency: that first
page contains exactly 100 distinct tickers, and offset 100 contains 92 distinct
tickers with 93 event rows. Offset 200 is empty. Across the pages there are 192
distinct tickers and 194 distinct events. PLLMF and PLLMD each carry two quarterly
announcements. This demonstrates that the observed endpoint paginates company
groups while expanding multiple events into rows; the current adapter incorrectly
uses event-row counts for page size, total and offset accounting.

The fix is to validate/count distinct tickers for pagination, advance offsets by
the company count, and retain every event row for event validation and repeated-pass
comparison. Keep terminal-empty evidence, stable totals/schema, request/row/deadline
bounds, cross-page group checks and same-CIK/date ambiguity rejection. Multiple
events for a company in the curated universe remain ambiguous; unrelated companies
with multiple events must not prevent a valid global page from being enumerated.
The adapter fix is implemented locally with regression and installed-container
coverage; production deployment remains pending. Cloud logs retain only the
exception type; this is a live reproduction using the production adapter rather
than a saved raw cloud response.

Local validation with the corrected adapter completed a read-only 30-day check
for all 72 reviewed companies (`2026-10-15` through `2026-11-13`) in 223 seconds,
using the existing 300-request/600-second/30000-row bounds. Yahoo supplied 45
observations for 45 companies; the report explicitly lists 27 companies without
observations. This verifies consistent collection for the requested scope, not
independent provider coverage of every company. The report is
`/private/tmp/finbot-yahoo-grouped-pagination-72-20261016.json`. No AWS/SEC requests
or durable calendar writes were made by this diagnostic.

The calendar alarms remain enabled. A complete validated refresh for all 72 CIKs
is needed to repair freshness and scope. The fix uses normal application delivery;
no CDK, environment, table schema, universe, or checkpoint reset is needed. After
activation, verify a new successful calendar checkpoint covering all 72 companies
and the expected dates, then confirm both alarms return to OK through normal
metric evaluation. A local read-only check does not update those checkpoints. Existing calendar observations and SEC
polling/recovery remain intact. New companies without calendar observations rely
on safety polling. Do not fabricate a success checkpoint or force alarms to OK.
See OPS-002 for follow-up.

Alarm histories and aggregated log evidence are saved outside Git at
`/private/tmp/finbot-calendar-alarm-investigation-20261016.json`; bounded public
pagination summaries are under
`/private/tmp/finbot-yahoo-pagination-evidence-20261016.json` and
`/private/tmp/finbot-yahoo-pagination-union-20261016.json`. No raw Yahoo
payloads, cookies or credentials are retained in these reports.

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
The application must already be running a v3-compatible image when using the
current helper (a retained v2 helper requires its matching compatible image).
This must be verified before the operation; the helper does not build or deploy an image. No CDK deployment,
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
   with the current selector's SELECT checkpoints and pending ACQUIRE eligibility. It verifies those
   checkpoints, preserved parents and existing selected children before reporting
   `queued`. The first enumeration timestamp/count remains unchanged; those counts
   describe the original snapshot, not today's number of children. The helper also commits the full reviewed filing membership, preserving the first
   enumeration timestamp/count. No S3/SNS/SQS, calendar, satisfaction or company
   writes occur in the helper. If interrupted,
   keep ingestion stopped and resume **the same manifest** with `--execute`;
   compatible partial creates/selection writes are idempotent. Unexpected conflicts
   stop the operation for review. Do not delete records or regenerate the preview
   to bypass a conflict.
4. Restore desired count one on the same service/task definition and wait for
   stability. Normal pending recovery downloads/stores the four raw attachments,
   publishes complete filing-ready events including existing selected files, and performs normal calendar satisfaction
   handling. Verify HEALTHY task/container state, four strong artifact records with
   `stored_at`, `s3_uri` and `published_at`, corresponding S3 keys, and no new terminal
   failures. Existing files are not downloaded again. Each new snapshot includes all selected
   stored files; previous notification payloads remain unchanged. Then restore delivery with
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
