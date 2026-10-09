# Phase 8 deployment and recovery runbook

The CDK application and delivery workflows are implemented and tested offline.
No AWS resources have been provisioned and no live AWS/SEC validation is implied.
Commands in the infrastructure/release sections below make real AWS changes; run
only against an explicitly authorized account/environment. Routine tests need no
AWS credentials and must not contact SEC or provider services.

## Tooling and offline validation

From the repository root, use Python 3.12+ and Node 24:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
python3.12 -m venv infra/cdk/.venv
infra/cdk/.venv/bin/python -m pip install -r infra/cdk/requirements-dev.txt
npm ci --ignore-scripts --no-audit --no-fund
.venv/bin/python -m compileall -q src tests scripts
.venv/bin/python -m pytest -q
infra/cdk/.venv/bin/python -m pytest -q infra/cdk/tests
npm run synth -- --no-lookups
.venv/bin/python -m build
```

The default synthesis uses `infra/cdk/config.example.json`, a fixture with dummy
account/contact/repository/image values. It never discovers an account or uses
AWS lookups. Do not deploy those example inputs. On restricted macOS filesystems,
set `JSII_RUNTIME_PACKAGE_CACHE=/private/tmp/finbot-jsii` for CDK commands/tests.
The CLI is pinned in package-lock.json; infrastructure dependencies stay in their
own virtual environment and are excluded from the wheel/image.

Validate the supported Linux ARM64 image:

```bash
docker build --platform linux/arm64 --provenance=false -t finbot-ingestion:phase8 .
FINBOT_CONTAINER_TESTS=1 FINBOT_CONTAINER_IMAGE=finbot-ingestion:phase8 \
  .venv/bin/python -m pytest -q tests/integration/test_phase7.py -m container
.venv/bin/python scripts/check_heartbeat_volume.py finbot-ingestion:phase8
.venv/bin/python scripts/install_actionlint.py /private/tmp/finbot-tools
/private/tmp/finbot-tools/actionlint -shellcheck= -pyflakes=
```

Image builds and tool installation fetch dependencies. Fixture containers have
network disabled and dummy credentials. The volume probe starts only a short
Python filesystem check, not ingestion. The pinned actionlint archive is checksum
verified against its [official release](https://github.com/rhysd/actionlint/releases/tag/v1.7.12).
CI executes the same offline checks on `ubuntu-24.04-arm`; confirm that this runner
is available for your repository before enabling delivery.

## Infrastructure configuration

Copy the example to ignored `infra/cdk/config.local.json`, replace every fixture
value and export `FINBOT_INFRA_CONFIG` as its absolute path. Fields:

| Field | Meaning |
| --- | --- |
| account / region | Explicit 12-digit target account and AWS region |
| environment / prefix | Lowercase resource names; metrics Environment uses environment |
| sec_user_agent | Identifying application/contact address, no credentials |
| github_subject | Exact `repo:OWNER/REPO:ref:refs/heads/main` or `repo:OWNER/REPO:environment:production` subject |
| image_digest | Real validated ARM64 ECR image digest, `sha256:...` |
| github_oidc_provider_arn | Optional existing account GitHub OIDC provider; import it instead of duplicating it |
| alarm_action_arn | Optional existing same-region/account SNS notification topic |
| monitoring_enabled | Default false; controls notification actions, not alarm evaluation |
| sec_error_threshold / publish_error_threshold | Error alarm counts per minute; defaults 10 / 3 |
| discovery_latency_ms / failed_work_age_seconds | Latency/queue-age thresholds; defaults 60000 / 300 |
| cpu / memory_mib | Default 512/1024; validated v0 Fargate combinations |
| calendar_provider | Explicit `placeholder` (default, unavailable) or `yahoo` |
| calendar_lookahead_days | Inclusive full scope; default 30, range 3–360 with repository lookback margin |
| calendar_full_refresh_seconds / calendar_near_term_refresh_seconds | Completion-based full cadence 86400; optional near-term cadence 0 (disabled) |
| yahoo_cache_dir | Private directory below writable `/tmp`, default `/tmp/finbot-yahoo` |

These fields are JSON configuration inputs, not `cdk -c` context overrides.
The task sets `CALENDAR_PROVIDER_ATTEMPTS=1` for Yahoo and 3 for placeholder.
Changing provider configuration does not activate the stopped service.

Verify your repository's actual OIDC subject before setup, including any immutable
subject customization. For an environment subject, restrict the production GitHub
environment to main and configure its protection rules. Branch subjects do not
encode environment approval; the workflow still uses the production environment.
No static AWS keys belong in variables, source or task definitions.

The default VPC uses two public subnets in region suffixes `a` and `b`, a task public
IP and HTTPS-only egress. Verify those AZs are supported in the target account.
There are no inbound rules, load balancer or ports. This avoids NAT for v0; private
subnets/egress require a reviewed infrastructure change, not an automatic fallback.
S3 has service-managed encryption/versioning; DynamoDB has on-demand capacity,
encryption, PITR and deletion protection. SNS uses a retained, rotating dedicated
KMS key so publisher permissions are scoped to one key. SQS uses service-managed
SSE and a 14-day retention period. KMS is supporting encryption infrastructure,
not an additional ingestion pipeline. Allow for its cost when sizing the environment.

Bucket policies reject writes lacking exact If-None-Match `*`. Ingestion cannot
remove objects/versions. No multipart/copy bypass or raw-data expiration is
configured. Data tables, bucket, topic, queue, KMS key and ECR are retained across
stack deletion/replacement; both stacks have termination protection. Do not rename
construct IDs or assume RETAIN makes resource replacement safe. Always review diff.
CloudWatch logs retain 30 days. ECR tags are immutable, scanning is requested on
push, and no lifecycle expiration can remove a release needed for rollback.

## Authorized first deployment

Infrastructure deployment is manual. Application workflows never run CDK deploy.
Keep repository-level `FINBOT_DELIVERY_ENABLED` unset/false initially.

1. Authenticate using the approved local AWS credential mechanism. Verify target
   account/region and authorize CDK bootstrap separately if needed.
2. Run `npm run cdk -- bootstrap aws://ACCOUNT/REGION` only after authorization.
3. Set the explicit local configuration; run strict synth and review
   `npm run cdk -- diff PREFIX-ENV-state`.
4. Deploy the state stack with `npm run cdk -- deploy PREFIX-ENV-state`.
5. Obtain the ImageRepositoryUri output. Build/test an ARM64 image, authenticate
   Docker to that ECR registry, push an immutable tag and record its resolved digest.
   Use approved SDK/CLI tooling; do not start an ingestion task as an image probe.
6. Put that digest in the infrastructure configuration. Review the runtime diff and
   manually deploy `PREFIX-ENV-runtime`. The service always starts at desired count
   zero. Record outputs for cluster, service, baseline task definition and roles.
7. Configure the GitHub environment/variables below. Enable image delivery only
   after verifying its exact target values and federation trust.

The runtime stack references retained state exports deliberately using strong
cross-stack references. Removing exports requires a separate migration; no
hotswap/express deployment bypass is supported. Stack output values are resource
references, not authorization to activate production.

The task runs non-root with a read-only filesystem. The image declares writable
`VOLUME /tmp`; the ECS ephemeral bind mount uses the same path and preserves image
permissions, as described by [AWS bind-mount guidance](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/bind-mounts.html).
Verify ownership/heartbeat on an authorized cloud check before activation. There
is no local shared-data mount and no test shim in the production image.

## GitHub delivery configuration

Repository variable `FINBOT_DELIVERY_ENABLED=true` enables the release job. In the
production GitHub environment, configure these variables from reviewed stack outputs:

- `AWS_REGION`, `FINBOT_RELEASE_ROLE_ARN`;
- `FINBOT_CLUSTER`, `FINBOT_SERVICE`, `FINBOT_REPOSITORY_URI`;
- `FINBOT_BASELINE_TASK_DEFINITION` (CDK's approved baseline ARN);
- `FINBOT_ACTIVATION_APPROVED=false` until all readiness gates below are satisfied.

CI runs on PRs and main pushes with contents-read permission and no AWS credentials.
Delivery responds only to successful same-repository main push CI runs or manual
main dispatch. Releases serialize without cancelling an in-progress handoff.
Actions use resolved official commit SHAs. Only the release job receives OIDC
permissions; it cannot provision infrastructure, start standalone tasks, delete
raw data or alter IAM. The image tag contains commit/run/attempt identity and the
service revision uses a resolved digest. Released images must remain available.

A stopped service receives a staged revision and stays stopped. An active service
requires the activation-approved flag for subsequent releases as well. Initial
activation additionally requires manual dispatch with `activate=true`; a normal
main push does not activate a stopped service. Optional `image_digest` selects an
existing validated ARM64 release for rollback, with the same stop/wait/start path.
No version-tag overwrite or mutable `latest` deployment is used.

`release.py` checks that the current task configuration matches the CDK baseline
apart from image and AWS response-only fields. A mismatch fails before stopping
anything. It registers the replacement first, scales to zero, paginates RUNNING
and desired-STOPPED tasks, observes known task statuses on two consecutive checks and waits
for every observed task to reach STOPPED before starting one. ECS APIs are eventual;
these checks do not provide a distributed exclusion guarantee for every failure.
It verifies the actual task revision, digest, container health and deployment
completion rather than accepting a generic services-stable result.

The release manifest records target, prior/new revisions, digest, source commit
and checkpoint/outcome; it excludes task settings, document bytes and credentials.
Failed releases stay failed even after successful restoration. SDK attempts/timeouts,
subprocess timeouts and a 1200-second deployment deadline bound waits; restoration
has its own deadline. Workflow timeout is 55 minutes. Forced runner termination can
interrupt restoration, so the manifest/manual recovery remain necessary.

## Production readiness and activation

The Yahoo adapter is implemented offline; production inputs and live verification
remain pending. Do not set the approval flag or start production until these are complete:

1. Verify complete 30-day scoped Yahoo coverage for the approved universe and
   resolve the ongoing access arrangement. Set `calendar_provider` to `yahoo` in
   reviewed CDK context/JSON configuration. The runtime uses keyless pinned yfinance,
   private `/tmp/finbot-yahoo` caches, 30-day daily refresh and replacement-only
   reconciliation. The default placeholder cannot establish readiness. Offline
   fixture tests and the earlier partial live probe do not satisfy this live gate.
2. Approve the authoritative company universe and explicitly authorize seeding.
   Use the existing repository Company contract (CIK padded to ten digits, ticker,
   name, enabled, repository_schema_version=1, enabled_marker=ENABLED for enabled
   rows). No sample records or seeding command were introduced by Phase 8.
3. Rebuild/test the final adapter image and approve its digest. Reconcile the CDK
   baseline/environment as needed before releasing it.
4. Perform explicitly authorized cloud checks: conditional PUT denies overwrite,
   object absence detection and checkpoint permissions work, heartbeat volume is
   writable, task-role credentials work and EMF reaches CloudWatch. Use test
   resources/fixtures where appropriate; live SEC checks remain manual/authorized.
5. Confirm resource retention, image retention, notification topic policy/delivery,
   memory headroom and actual subnet/AZ/egress behavior. Two artifact workflows can
   temporarily retain multiple 64-MiB byte buffers; 1 GiB is initial sizing, not a
   measured production guarantee.
6. Set monitoring_enabled true in a reviewed manual runtime deployment and verify
   alarm routing. Set FINBOT_ACTIVATION_APPROVED true only after readiness approval.
   Then manually dispatch activation. Verify a healthy task, configured/fresh full
   calendar coverage and resumed polling; task HEALTHY alone is not readiness.

## Automatic recovery and alarms

[ADR 008](adr/008-use-conservative-ecs-automatic-recovery.md) is implemented:
active count 1, min/max 0/100, 120s stop timeout, 150s SEC quiet startup, 300s ECS
health start period, 30s checks, 5s check timeout and three retries. Local defaults
retain zero quiet delay and the Docker health check's 120s startup allowance.
No SEC requests occur during the quiet period. Shutdown refuses new HTTP attempts,
including retries/redirects, while already dispatched I/O/checkpoints can finish.

ECS automatically replaces crashed/unhealthy tasks. The quiet period is conservative
protection; it is not formal exclusion. It alone adds 2.5 minutes before polling
resumes; detection, shutdown and provisioning add more. During expected earnings
this can materially miss the seconds-scale discovery objective. Measure recovery
components and revisit [ARCH-001](BACKLOG.md#arch-001--reduce-recovery-delay-during-active-earnings-windows).
Calendar stale/unconfigured state alarms without failing otherwise live task health.

EMF dimensions are Service=finbot-ingestion and Environment=config.environment.
Liveness alarms after three missing/unhealthy one-minute periods; calendar stale,
scope mismatch and provider-unconfigured alarms need no CalendarSyncAgeSeconds
sample. Queue visible/inflight counts above zero and age above 300s surface failed
work. SEC errors (10/min), publication failures (3/min), terminal failures and
DiscoveryLatencyMs above 60,000 have initial alarm thresholds configurable in the infrastructure JSON.
Non-liveness sparse data is not breaching. Latency is SEC acceptance-to-discovery,
not measured public availability or publication time. No default RunningTaskCount
metric, Container Insights charge or metrics sidecar is assumed.

Stopped environments still evaluate alarms; actions are disabled by default.
Planned handoffs can breach liveness. Operators should understand this outage rather
than infer data loss. Configure an existing action topic and its CloudWatch publish
permissions; no email/subscription is created automatically.

## Manual infrastructure changes and release recovery

Before runtime infrastructure changes, serialize against delivery, disable the
repository delivery flag, scale to zero using approved AWS tooling, and wait for
all observed task ARNs to reach STOPPED. Preserve the deployed digest in local CDK
configuration before diff/deploy; otherwise CDK can revert the application image.
CDK always returns the service to desired zero. Update the baseline GitHub variable
from the new stack output, then explicitly reactivate through the release procedure
if readiness remains approved. This is deliberate application-revision drift from
the CloudFormation baseline; inspect it, never blindly reconcile against an old image.

After a failed/cancelled workflow, inspect the manifest and actual ECS service/tasks:

- `failed_restored`: verify the previous revision/digest/health before treating
  service as recovered; the failed workflow should remain failed.
- `failed_restore_unverified` or missing checkpoint: disable delivery, set desired
  zero and wait for all observed containers to STOPPED. Do not launch while shutdown
  is unproved. Inspect logs/events and restore a retained known-good image through
  the same release path once quiescence and approved configuration are established.
- `staged`: count zero is intentional; an image was delivered without activation.

The release role has no broad data access. Do not investigate by deleting raw
objects or resetting terminal checkpoints. Correlate ingestion.work_failed envelopes
with durable filing/artifact terminal records. The queue may expire after 14 days;
checkpoints persist. Operator terminal redrive remains deferred.

## IAM verification limits

The IAM analyzer was run locally with telemetry disabled. Reproduce its baseline:

```bash
DISABLE_IAM_POLICY_AUTOPILOT_TELEMETRY=true uvx iam-policy-autopilot@0.3.0 generate-policies \
  scripts/release.py scripts/deliver.py --service-hints ecs ecr --pretty
```

It recognizes literal delivery calls but misses many application calls dispatched
through shared executors/getattr. Its generic output is not deployable least
privilege. CDK enforces the approved LLD actions/resource contract, with tests and
AWS documentation checks. Current AWS service authorization JSON supports family
scoping for RegisterTaskDefinition despite older policy examples using `*`.
ListTasks needs `Resource=*` with an exact ecs:cluster condition; DescribeTaskDefinition
and ECR GetAuthorizationToken also use wildcard resources. PassRole remains limited
to the two ECS roles, and UpdateService to this service. No ECS registration tags
are requested, so release does not grant TagResource. Review synthesized policies
and perform authorized permission checks before claiming live IAM validation.
