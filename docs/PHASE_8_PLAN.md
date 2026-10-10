# Phase 8 implementation plan — CDK and application delivery

Status at implementation completion: IMPLEMENTED (2026-10-09); validated offline,
not yet deployed at that point. Current deployment status is in DEPLOYMENT.md.

Subsequent decision: [ADR 010](adr/010-use-service-managed-encryption-without-kms-integration.md)
supersedes the original SNS/DynamoDB KMS choices described below. Current code
uses SSE-S3, DynamoDB AWS-owned encryption and SSE-SQS, with SNS unencrypted at
rest. The customized checked-in bootstrap also uses SSE-S3. Original Phase 8
delivery/validation below is historical; current commands are in DEPLOYMENT.md.

Prepared against clean repository revision `6c5a2ac`. This plan supplements HLD,
LLD, ADRs 001–007 and MIGRATION_PLAN. The initial request authorized planning, followed by explicit implementation
authorization. Infrastructure provisioning, company seeding and live AWS/SEC operations require
separate authorization. No resources were inspected or modified while planning.

## Delivered scope and validation

The user subsequently authorized Phase 8 implementation. The plan below remains
the original planning record; this section describes the delivered result.

- Added independent pinned CDK v2/Node tooling, explicit configuration and offline
  state/runtime synthesis. State retains bucket, four tables/GSIs, encrypted
  messaging, SNS encryption key and immutable ECR; runtime stays stopped by default.
- Added task/execution/GitHub OIDC role separation, create-only storage enforcement,
  no-inbound HTTPS egress, ARM64 Fargate settings, health and eleven EMF/SQS alarms.
  Error/latency/queue-age thresholds are configurable in infrastructure JSON.
- Implemented ADR 008 startup quiet timing and stop admission for new SEC requests,
  including retry/redirect/limiter boundaries. Added metrics environment validation.
  Docker `/tmp` VOLUME preserves non-root write access for heartbeat copy-up.
- Added pinned-action CI/delivery, image digest validation, explicit stop/wait/start
  helper, bounded rollback/restoration, release manifests, readiness gates and
  stopped-environment preservation. Helpers are outside the runtime distribution.
- Added failure-oriented runtime/release tests, CDK/workflow assertions, checksum
  verified actionlint installation and an offline heartbeat-volume probe.
- Updated README/HLD/LLD/migration/ADR and added DEPLOYMENT.md; backlog preserves
  the future review of recovery latency during active earnings.

Material implementation choices:

- ECR belongs to the retained state stack so the first real image is available
  before runtime deployment. A dedicated retained KMS key encrypts SNS and allows
  narrowly scoped publisher permissions; other data uses service-managed encryption.
- The supported v0 topology creates public subnets with HTTPS egress and no ingress.
  Importing private VPC/subnet egress was deferred rather than adding another
  unvalidated deployment path. Target region AZ suffixes a/b must be checked before
  live deployment. Strong state exports are explicitly configured.
- Current AWS authorization JSON supports RegisterTaskDefinition on the task-family
  ARN, unlike older documentation examples. ListTasks is scoped by ecs:cluster
  condition; the image helper receives explicit ecr:DescribeImages permission.
  IAM analyzer output was reviewed as a baseline; dynamic adapters limit coverage.
- Single-platform image builds disable provenance attachment to avoid publishing
  a manifest index where the release contract requires a validated ARM64 image.
- Initial activation is a manual delivery input plus a separately approved readiness
  variable. Routine image delivery preserves zero desired count. CDK runtime
  maintenance always leaves the service stopped for explicit restart.

Executed checks and results are recorded below. They exercise offline boundaries
and local containers, not deployed AWS services or GitHub workflow execution.

Final validation on 2026-10-09:

| Command | Result |
| --- | --- |
| `.venv/bin/python -m pytest -q` | 473 passed; 6 opt-in container cases skipped |
| `infra/cdk/.venv/bin/python -m pytest -q infra/cdk/tests` | 15 passed |
| `JSII_RUNTIME_PACKAGE_CACHE=/private/tmp/finbot-phase8-jsii npm run synth -- --no-lookups` | Strict, credential-free synthesis passed |
| `/private/tmp/finbot-phase8-tools/actionlint -shellcheck= -pyflakes=` | Both workflows passed actionlint |
| `.venv/bin/python -m build` | Wheel and source distribution built successfully |
| `docker build --platform linux/arm64 --provenance=false -t finbot-ingestion:phase8 .` | ARM64 image rebuilt successfully |
| `FINBOT_CONTAINER_TESTS=1 FINBOT_CONTAINER_IMAGE=finbot-ingestion:phase8 .venv/bin/python -m pytest -q tests/integration/test_phase7.py -m container` | 6 passed; 9 non-container cases deselected |
| `.venv/bin/python scripts/check_heartbeat_volume.py finbot-ingestion:phase8` | Non-root, read-only image wrote its heartbeat in the anonymous `/tmp` volume with networking disabled |

Compilation and whitespace checks also passed. The tested local ARM64 image ID
was `sha256:f4d915ccabef0303e9a079f5517be7e3451e800b7a1cb7b93c3b3947590dd3bd`.
This local image ID is not an ECR manifest digest or a deployed release.

CloudFormation deployment, actual OIDC authentication, ECS lifecycle behavior,
alarm delivery, real AWS permissions and live SEC/provider ingestion remain
unverified. The release tests use stateful fake clients and SDK shape validation;
they do not establish a global SEC admission lock. Production activation still
requires a selected calendar adapter, approved company universe and separately
authorized live verification.

## Intended result

Deliver Python CDK infrastructure under `infra/cdk/`, offline infrastructure and
delivery tests, GitHub Actions application CI/delivery, and a practical deployment
runbook. Keep infrastructure deployment manual and application releases separate.
Preserve the acquisition/event/schema contracts and one shared SEC request budget.
Automatic recovery follows accepted [ADR 008](adr/008-use-conservative-ecs-automatic-recovery.md),
including its documented overlap limitation and future latency review.
Consumer queues, extraction, provider selection and universe selection remain
separate work.

Phase 8 code can be built and validated before the external inputs are available.
An initially stopped service permits infrastructure preparation without starting
ingestion. Production activation requires a selected implemented calendar adapter
and an authoritative enabled company universe; the placeholder cannot establish
calendar readiness. Do not mark live deployment verified from offline tests.

## Proposed decisions and configuration

These are implementation proposals, except the accepted recovery policy in ADR
008. None are claims about deployed resources.

| Concern | Proposed v0 choice |
| --- | --- |
| Infrastructure | CDK v2 in Python; independently pinned infrastructure dependencies and CLI; separate infra virtual environment |
| Stack boundaries | Retained state stack and runtime/delivery stack in the same CDK application |
| Environment | Explicit account, region, environment name and resource prefix; no inferred production values |
| Networking | Small VPC with public subnets, public task IP for outbound access, no ingress rules or exposed ports; no NAT gateway initially |
| Compute | Linux ARM64, 512 CPU units and 1024 MiB memory as an initial configurable sizing proposal; validate image and measured memory before activation |
| Service | Fargate on-demand, replica service; desired count restricted to 0 or 1; no autoscaling, Spot, standalone ingestion tasks or blue/green deployments |
| Deployment | Explicit stop/wait/start procedure; `minimumHealthyPercent=0`, `maximumPercent=100` as additional constraints |
| Automatic recovery | ECS stop-first replacement, 120-second stop timeout and 150-second SEC startup quiet period; conservative protection, not formal exclusion |
| Bootstrap state | Desired count 0; no automatic start from a repository push until environment activation is configured |
| Data | S3 versioning/encryption, DynamoDB on-demand capacity, PITR and deletion protection; retained data resources |
| Encryption | Service-managed encryption initially; customer-managed KMS keys only when requirements justify them |
| Retention | Raw objects and metadata retained indefinitely; logs initially 30 days; operational SQS retention 14 days |
| Images | Immutable ECR tags with commit/run identity; deploy resolved digests; preserve rollback images |
| Notifications | Optional existing alarm-action ARN; alarms exist even when no notification destination is configured |

Public task addressing is an outbound-connectivity choice, with no public API or
load balancer. Document the tradeoff and support an explicitly supplied VPC/subnet
configuration for private egress when required. Confirm the target region,
networking choice, repository identity and sizing before an actual deployment.
Do not invent account IDs, company records, provider credentials or alert recipients.

## 1. Establish CDK layout and reproducible offline synthesis

Add:

- `infra/cdk/app.py`, `config.py`, `state_stack.py`, `ingestion_stack.py`;
- `infra/cdk/requirements.txt` and development requirements with compatible pins;
- root `cdk.json` and a pinned CDK CLI installation mechanism;
- `infra/cdk/tests/` for CDK assertions;
- ignore rules for infra virtual environments, CDK output and temporary release files.

Use stable construct IDs and explicit stack termination protection. Synthesis
must work against fixed fixture account/region values without credentials, context
lookups, image publication, Docker asset builds or AWS calls. Provide a clearly
named example configuration with safe dummy values; keep deployment-specific
values outside tracked secrets. Infrastructure dependencies must not enter the
application wheel or production image.

Reject unsafe configuration early: count above one, mismatched region/resources,
unsupported CPU architecture, missing SEC identity, and incomplete OIDC identity.
Treat an existing account-level GitHub OIDC provider as an explicit import option
to avoid attempting to create a duplicate.

**Exit gate:** deterministic synthesis and configuration tests pass offline.

## 2. Provision retained storage, metadata and messaging contracts

State stack owns the artifact bucket, four DynamoDB tables, standard ArtifactReady
SNS topic and standard operational failed-work SQS queue. Apply encryption,
HTTPS-only resource policies where supported, public-access blocking, retention
and replacement protection. Do not add consumer subscriptions or queues.

Implement exactly the LLD section 6.5 keys/indexes; all key attributes are strings:

| Table | Base keys | KEYS_ONLY indexes |
| --- | --- | --- |
| Companies | `cik` | EnabledCompanies: `enabled_marker` / `cik` |
| Calendar | `expected_date` / `cik` | None |
| Filings | `accession_number` | PendingFilingEnumeration: `pending_work_kind` / `pending_work_sort` |
| Artifacts | `artifact_id` | PendingArtifactWork: `pending_work_kind` / `pending_work_sort`; ArtifactsByAccession: `accession_number` / `filename` |

No TTL, streams, scans, new analytical indexes or age-based recovery filters.
Calendar permissions must include `__calendar_sync__` and
`__event_satisfaction__`, not just date partitions.

Bucket policy must explicitly deny nonconditional writes by ingestion and deny
object/version deletion by its role. The role allows only conditional single PUT
with `s3:if-none-match="*"`, required object reads and bucket listing for accurate
HEAD absence detection. Do not grant `grant_read_write()` blindly or permit an
If-Match overwrite alternative. Test both the Allow and explicit Deny conditions;
encryption/versioning alone do not enforce immutable application writes.
Use bucket-level listing permission if the existing adapter's HEAD behavior needs
it; never add a prefix restriction that changes missing-object errors into 403s.

Retain the bucket and tables on deletion/replacement; disable automatic bucket
emptying and raw-data expiration. Retain the topic, failed-work queue and ECR
repository to preserve integrations/operational history across stack removal.
SQS retention is bounded, so durable terminal checkpoints remain the source of
truth. Document separate downstream subscription ownership and access grants.

**Exit gate:** schema, retention, encryption and immutable-write assertions pass;
runtime adapters still use their existing resource/schema contracts.

## 3. Define IAM, runtime and observability

Separate three roles:

1. **Task role:** table-specific GetItem/Query and only necessary PutItem/UpdateItem
   actions; index Query; conditional S3 PUT, GetObject/ListBucket; SNS Publish and
   operational SQS SendMessage. Company configuration is read-only at runtime.
   No Scan/DeleteItem, object deletion, queue consumption or resource provisioning.
2. **Execution role:** pull the specified ECR repository and write the specified
   log group. Add scoped secret access only when a selected adapter needs it.
3. **GitHub release role:** push to this ECR repository, read deployment state,
   register approved task-family revisions, update this service, inspect/wait for
   its tasks and pass only the task/execution roles to ECS. No CDK deploy, broad
   IAM mutation, application data access or standalone RunTask permission.

Document unavoidable wildcard-resource actions separately and constrain them
with supported condition keys. Verify action/resource compatibility through AWS
documentation during implementation. ECS role trust uses supported source-account
and source-ARN protections. OIDC trust requires the exact repository subject and
`aud=sts.amazonaws.com`; use a protected GitHub environment with main-only branch
rules if choosing an environment subject. Verify the repository's actual OIDC
subject format rather than assuming it. PR jobs get no release credentials.

Runtime stack owns networking, cluster, ECR, log group, task definition, service,
release role and alarms. Preserve UID/GID 10001 and direct PID 1 module entry point.
Use read-only root storage, dropped capabilities, no inbound ports, writable
ephemeral heartbeat storage and `stopTimeout=120`. Ensure the mounted heartbeat
directory is writable by UID 10001; test volume ownership rather than assuming it.
Fargate volume configuration must not use unsupported Docker tmpfs settings.

Explicitly put the existing health command in the ECS task definition; Dockerfile
HEALTHCHECK alone is insufficient for ECS health management. Use interval 30s,
timeout 5s, start period 300s and three retries. The longer ECS allowance covers
the accepted startup quiet period; the current local image's 120s allowance is a
Phase 7 contract and must be reviewed if enabling the delay locally.
Process liveness remains separate
from calendar readiness. Task-role credentials replace local credential injection.
Inject resource names/ARNs and public runtime settings without static AWS keys.

Add a configurable SEC startup quiet period (150s in ECS, default 0 locally),
with finite nonnegative validation, monotonic timing and interruptible shutdown.
Before it elapses, no SEC dispatch may occur through polling, enumeration,
acquisition, recovery, retries or redirects. Add a thread-safe SEC admission guard
which closes at stop request/SIGTERM and rechecks immediately before each actual
HTTP attempt, including after limiter waits. Finish already dispatched calls and
safe checkpoints; defer queued SEC work without terminalizing it. Do not close the
shared HTTP session while an in-flight call uses it. Test startup cancellation,
queued work, limiter waits, backoff and repeated shutdown against fake clocks and
blocked transport. Add settings/docs only when implementing them.

Expose a validated metrics environment setting, default
`local`, and wire it into the Metrics instance in the runtime factory. Match alarms
to namespace `Finbot/Ingestion`, Service `finbot-ingestion` and the configured
Environment. Add an offline factory/configuration test and update `.env.example`,
README and LLD. Keep existing metric names and event schemas.

Use awslogs to collect stderr JSON logs and stdout EMF without a metrics sidecar.
Initial configurable alarms:

- missing/unhealthy RuntimeHealthy heartbeat, including missing-data treatment;
- calendar stale, scope mismatch or provider unconfigured; never-synced state
  must alarm even when CalendarSyncAgeSeconds has no sample;
- operational queue visible/inflight backlog and oldest-message age;
- TerminalFailures, repeated SecRequestErrors and ArtifactPublishFailures;
- sustained elevated DiscoveryLatencyMs, with sparse-sample handling and explicit
  acceptance-time semantics, not a promise of public-availability latency.

Use native ECS metrics only where actually emitted; do not invent a default
RunningTaskCount metric or enable expensive telemetry solely to support it.
Separate intentionally stopped/bootstrap environments from activated monitoring.
Document thresholds, missing-data behavior and the planned deployment alarm gap.

**Exit gate:** permissions, task settings, metrics dimensions, health checks and
alarms match offline assertions and the rebuilt container checks.

## 4. Implement application CI and deployment orchestration

Add `.github/workflows/ci.yml` and `deploy.yml`. Pin third-party actions to reviewed
commit SHAs. CI runs offline application tests/compilation, build/package checks,
infra tests and strict synth, workflow validation and a target-architecture image
build. Run all six existing opt-in container lifecycle tests in CI against that
image; ordinary pytest skips do not replace these checks. Build tooling can fetch
dependencies, while runtime tests retain network-disabled fixture boundaries.

Delivery triggers on main only after equivalent validation succeeds, plus a
manual release/rollback entry point. Serialize releases by environment with
`cancel-in-progress: false`. Obtain OIDC credentials only in the release job.
Require an explicit activation variable; while inactive, delivery may push an
image but must not raise service desired count from zero.

Add a testable release helper under `scripts/` with a bounded deadline:

1. Validate target service, region, family, roles, deployment configuration and
   current count. Capture the previous task-definition ARN/digest and desired count.
2. Build/push an immutable commit/run tag; resolve and verify its ECR digest and
   architecture. Register a revision preserving approved environment, health,
   roles, volumes and task settings, changing only the application image.
3. Scale the active service to zero. Paginate task listings, capture all old task
   ARNs and wait until they are **STOPPED**, not just absent from RUNNING listings.
   Recheck for newly observed tasks and fail closed if quiescence cannot be proved.
4. Attach the new revision while stopped, then start one task if the environment
   was already active and release activation is permitted.
5. Wait for stability and verify the actual running task's revision/image digest,
   container health and completed deployment. A generic services-stable waiter
   alone is insufficient; the service could stabilize on a rolled-back revision.
6. On failure after interruption, report the checkpoint and exit nonzero. Attempt
   bounded restoration of the captured revision only after stopping and waiting
   for any replacement task. If restoration fails or is cancelled, leave a clearly
   reported stopped/unverified service for the manual recovery procedure.

Finish all build/registration checks before stopping the existing task. Do not use
an ordinary rolling-deploy action that starts a new ingestion process first.
Manual rollback follows the same stop/wait/start path. Keep release manifests
(commit, tag/digest, prior/new revision and outcome) as workflow artifacts, without
credentials or data payloads. Preserve released images needed for rollback; defer
aggressive ECR cleanup until its retention policy protects deployed releases.

CDK and ECS releases need an explicit drift contract: CDK owns the baseline task
configuration, ECS release jobs own application revisions. Before manual runtime
stack changes, stop the service, reconcile the deployed image/revision into the
CDK input, inspect diff, apply infrastructure, then restart through the runbook.
Test that this path cannot silently revert the image or start a second process.
Never run infrastructure deployment concurrently with an application release.

**Accepted automatic-recovery limit:** ECS maximumPercent counts RUNNING/PENDING
tasks; it does not prove a STOPPING container has finished cleanup. ADR 008 accepts
stop-first automatic replacement with a 150-second startup quiet period and SEC
shutdown admission guard as conservative v0 protection, without a formal exclusion
guarantee. The release helper's STOPPED gate remains required for controlled
deployments, rollback and manual infrastructure updates. The quiet period alone
adds 2.5 minutes to recovery and may materially delay active-window earnings
discovery; total recovery can be longer. Measure and revisit through
[BACKLOG.md, ARCH-001](BACKLOG.md#arch-001--reduce-recovery-delay-during-active-earnings-windows).
Do not silently add distributed coordination or additional ingestion tasks.

**Exit gate:** stateful offline delivery tests cover successful replacement,
rollback, stopping tasks, pagination, timeout, lost update acknowledgment,
unexpected active count, incorrect digest, circuit-breaker rollback, cancellation
and preservation of stopped environments. Workflow failure always remains failure.

## 5. Runbook, external inputs and activation

Add `docs/DEPLOYMENT.md` covering:

- required account/region, GitHub repository/environment/OIDC identity, SEC contact,
  network selection, alarm actions and initial sizing;
- manual bootstrap, synth/diff/deploy ordering; deploy state first, push a real
  validated image, then deploy runtime at count zero;
- explicit universe seeding authorization and exact company schema/index marker;
  no sample-universe seeding or new universe-management command in Phase 8;
- calendar adapter selection/implementation as separate work before production;
- target-image checks, volume permissions, IAM/conditional-write checks, EMF
  visibility, notification routing and readiness before activation;
- stop/wait/start deployment, manual infrastructure maintenance, rollback and
  recovery after a workflow stops partway through;
- interpreting failed-work envelopes/checkpoints without deleting raw objects;
- the operational/cost tradeoffs of one task, egress and retained storage;
- optional live smoke-test checklist, clearly separated from routine offline CI.

No live smoke test runs as a planning or default implementation side effect.
Any authorized cloud verification records real outcomes separately from mocks.
Update README with actual supported commands/settings. Add the delivered Phase 8
contracts to LLD and update MIGRATION_PLAN with actual progress/results only.
Update HLD/ADRs if an accepted architecture decision changes.

## Validation and completion

During implementation run repo-local compilation, the full application pytest
suite, distribution build/import checks and `git diff --check`. Use the separate
infra environment for assertions and credential-free strict synth. Rebuild the
target image after application, dependency or container changes and run the
existing opt-in Docker suite against it. Test orchestration with stateful offline
AWS boundaries; any Python AWS helper must follow the installed SDK Python skill.

Completion requires all Phase 8 migration acceptance criteria: correct contracts,
retained immutable data, scoped permissions, observable health, verified controlled
single-process deployment, failing CI on failure, digest-identified releases and
manual infrastructure workflow. Record exact executed commands/results and
limitations. Code completion, live deployment verification and production readiness
are separate statuses; unresolved provider/universe inputs must remain explicit.

## AWS references checked during planning

AWS MCP documentation was consulted read-only on 2026-10-09:

- [ECS deployment upper bounds](https://docs.aws.amazon.com/sdk-for-kotlin/api/latest/ecs/aws.sdk.kotlin.services.ecs.model/-deployment-configuration/-builder/maximum-percent.html)
  — bounds RUNNING/PENDING tasks; motivates the separate STOPPED wait.
- [S3 conditional-write policy enforcement](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes-enforce.html)
  — conditional-header policy keys; choose create-only enforcement rather than
  permitting If-Match replacement or unrelated multipart exceptions.
- [Fargate task definition parameters](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_definition_parameters.html)
  — roles, platform and filesystem/volume configuration.
- [GitHub OIDC scoped trust example](https://docs.aws.amazon.com/securityagent/latest/userguide/sample-cicd-github.html)
  — repository subject and audience restrictions; verify actual subject at setup.

Installed AWS CDK, containers and IAM skills were also reviewed. Implementation
must reverify the specific CDK properties, supported IAM conditions and ECS
lifecycle behavior before treating synthesized templates as deployment-ready.
