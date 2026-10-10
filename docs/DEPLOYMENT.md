# Phase 8 deployment and recovery runbook

The CDK application and delivery workflows are implemented and tested offline.
On 2026-10-09 the operator reported successful creation of the shared `CDKToolkit`
bootstrap stack in account `559007813222`, region `us-east-1`, using the saved
SSE-S3 template, administrative profile and termination protection. The preceding
AWS `validate-template` command also succeeded. The operator subsequently supplied
read-only `describe-stacks` output confirming `CREATE_COMPLETE`, termination
protection enabled, bootstrap version `32`, and the expected bucket/repository
outputs. The operator subsequently supplied successful deployment output for
`finbot-prod-state`, after reviewing its additions-only template diff. The
operator subsequently reviewed and successfully deployed `finbot-prod-runtime`.
All three stacks are deployed. Operator-supplied read-only ECS output confirmed
service `ACTIVE`, desired/running/pending counts all zero, no API failures and
baseline task-definition revision `1`. Read-only task-definition output also
confirmed `ACTIVE`, Linux ARM64 and the exact published ECR image digest. Basic
infrastructure/deployed-image verification is complete; live task/application
AWS/SEC validation and activation remain pending. GitHub delivery has been enabled
and its first staged release succeeded, with ECS revision 2 and no running tasks.
The GitHub production environment, branch rule and target variables have been
configured and verified as recorded below.
Commands in the infrastructure/release sections below make real AWS changes; run
only against an explicitly authorized account/environment. Routine tests need no
AWS credentials and must not contact SEC or provider services.

State-stack outputs recorded from the operator's 2026-10-09 deployment:

| Output | Value |
| --- | --- |
| ArtifactBucket | `finbot-prod-state-artifacts82dd59a1-8gt63rcgmaua` |
| CompaniesTable / CalendarTable | `finbot-prod-companies` / `finbot-prod-calendar` |
| FilingsTable / ArtifactsTable | `finbot-prod-filings` / `finbot-prod-artifacts` |
| ArtifactTopicArn | `arn:aws:sns:us-east-1:559007813222:finbot-prod-artifact-ready` |
| FailedWorkQueueUrl | `https://sqs.us-east-1.amazonaws.com/559007813222/finbot-prod-failed-work` |
| ImageRepositoryUri | `559007813222.dkr.ecr.us-east-1.amazonaws.com/finbot-prod-ingestion` |

Image preparation (2026-10-09): the operator built
`finbot-ingestion:initial-20261009-1` using `--platform linux/arm64 --provenance=false`.
The exact image passed all eight container tests (nine non-container cases
deselected) in 23.77 seconds, using network-disabled fixtures. The operator also
confirmed that the separate image-declared `/tmp` volume permission check passed.
Local image checks are complete. The operator authenticated Docker to ECR and
successfully pushed tag `initial-20261009-1` to the application repository above.
The push reported digest
`sha256:2066b10a21c57eb9a263ca6154eba08d7c3fd98fd4214915289c36998300b1f2`,
matching the local build's manifest digest. Operator-supplied ECR output confirmed
the same digest and scan status `COMPLETE`, with one `HIGH` finding:
`CVE-2026-85091` in Debian's zlib package. The registry-confirmed digest is now
in the ignored local runtime configuration for stopped-runtime preparation.
The scan is not clean and the finding remains open; see the assessment below.
The bootstrap's asset repository is separate.
The operator reviewed the additions-only runtime diff, confirmed CDK's IAM
approval prompt and successfully deployed `finbot-prod-runtime`. The template
specifies desired count zero; the operator subsequently confirmed zero desired,
running and pending tasks using read-only `ecs describe-services`, with no API
failures, service status `ACTIVE` and task definition `finbot-prod-ingestion:1`.
Activation has not been approved or performed.

Runtime outputs recorded from the operator's 2026-10-09 deployment:

| Output | Value |
| --- | --- |
| ClusterName / ServiceName | `finbot-prod` / `finbot-prod` |
| TaskFamily | `finbot-prod-ingestion` |
| BaselineTaskDefinitionArn | `arn:aws:ecs:us-east-1:559007813222:task-definition/finbot-prod-ingestion:1` |
| ReleaseRoleArn | `arn:aws:iam::559007813222:role/finbot-prod-runtime-ReleaseRoleCAEFCF19-bb4IY5zrnh4M` |
| TaskRoleArn | `arn:aws:iam::559007813222:role/finbot-prod-runtime-TaskRole30FC0FBB-qlMezUWRPAdq` |
| ExecutionRoleArn | `arn:aws:iam::559007813222:role/finbot-prod-runtime-ExecutionRole605A040B-l4FWcg3aHqHV` |
| LogGroupName | `/finbot/finbot-prod` |

### Initial image vulnerability assessment (2026-10-09)

An offline check of the exact local image found Debian 13 (trixie), installed
`zlib1g:arm64` version `1:1.3.dfsg+really1.3.1-1+b1`, and Python reporting zlib
build/runtime version `1.3.1`. ECR reports the corresponding zlib source package
version `1.3.dfsg+really1.3.1-1` and HIGH severity. Debian's
[tracker](https://security-tracker.debian.org/tracker/CVE-2026-85091) still lists
trixie affected and no fixed Debian package. Upstream's
[fix](https://github.com/madler/zlib/commit/df84af25dc1942490e1d1c899a07619152a46148)
is available, but the scope of older versions remains under discussion in
[issue 1310](https://github.com/madler/zlib/issues/1310) and
[issue 1292](https://github.com/madler/zlib/issues/1292). Do not infer that 1.3.1
is unaffected merely from the CVE description's narrower version range.

The repository source contains no direct `gzwrite`, `gzprintf` or `gzvprintf`
calls. The SEC client requests gzip/deflate HTTP responses; that observation is
not proof that every native dependency cannot reach a vulnerable code path.
The finding is not suppressed or accepted as a false positive. No custom zlib
build, base-distribution switch or unverified package upgrade was introduced.

Recommendation: continue preparing the runtime with desired count zero, retaining
the finding for explicit review before activation. No containers run in that
stopped ECS service. Before activation, recheck the Debian fix status and ECR scan;
if a fix is available, rebuild, retest and publish a new immutable image tag/digest.
If proceeding with an unresolved finding, record the operator's explicit decision
and follow-up conditions. This assessment does not itself approve activation.

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

Local deployment preparation (2026-10-09): an ignored configuration was created
for account `559007813222`, `us-east-1`, prefix `finbot`, environment `prod`, Yahoo
calendar and the repository's GitHub `production` environment subject. The SEC
identity was reused from local `.env` and is not recorded here. The image digest
was initially a placeholder for state-stack preparation and was subsequently
replaced with the registry-confirmed digest above. Runtime preparation still
requires OIDC/AZ checks and the remaining runbook steps; the open image finding
must be reviewed before activation. Each machine must
prepare its own local configuration; the file is not committed.
Credential-free strict synthesis of this local configuration passed; both
application templates passed encryption-policy checks, with zero lint errors
and the previously reviewed CDK-generated redundant-dependency warning. The
operator reviewed `cdk diff finbot-prod-state --method template`, then successfully
deployed only the state stack with `--exclusively`. `FINBOT_INFRA_CONFIG` selects
the local file. Do not deploy the runtime template with the placeholder image digest.

Runtime prerequisite check (2026-10-09): the operator's read-only
`iam list-open-id-connect-providers` returned `[]`. No existing GitHub provider
needs to be imported; leave `github_oidc_provider_arn` unset so the runtime CDK
stack creates it. The release role's trust is restricted to audience
`sts.amazonaws.com` and subject
`repo:ilancooke/finbot-filings:environment:production`. Operator-supplied EC2 output
confirmed `us-east-1a` and `us-east-1b` are both `available`. This was the initial
trust subject; the GitHub verification below requires a correction. The provider itself grants no
permissions; the scoped release role defines permitted AWS actions.

With the registry-confirmed image digest, credential-free strict synthesis and
encryption-policy validation passed again for both application templates. Lint
reported zero errors and the one previously reviewed CDK-generated redundant
dependency warning. Direct template checks confirmed ARM64, the exact image digest
and service desired count zero. The operator reviewed only the runtime stack using
`cdk diff finbot-prod-runtime --exclusively --method template`, then successfully
deployed it with `--exclusively`. Read-only ECS zero-task verification passed.
Read-only `ecs describe-task-definition` confirmed the baseline is `ACTIVE`, uses
Linux ARM64 and references the exact published image digest. Next prepare GitHub
delivery configuration. The open image finding still requires activation review.

GitHub prerequisite verification (2026-10-09): authenticated read-only GitHub CLI
inspection confirmed `ilancooke/finbot-filings` is public, the default branch is
`main`, and the operator has administrator access. No deployment environments or
Finbot repository variables exist yet, so delivery remains disabled. The OIDC
settings report `use_default=true`, `use_immutable_subject=true`, and subject prefix
`repo:ilancooke@8453151/finbot-filings@1341430877`. The ignored local CDK configuration
now uses the exact production subject
`repo:ilancooke@8453151/finbot-filings@1341430877:environment:production`.
The operator reviewed the single release-role trust change and successfully
deployed the runtime stack correction with `--exclusively`. The baseline task
definition remains revision 1; the output resource names and role ARNs are unchanged.
The operator created the production environment with `ilancooke` as required
reviewer (self-review allowed for the sole operator), then created the `main`
branch rule. Read-only inspection confirmed that is the only deployment rule.
Environment ID is `23924540932`; branch-policy ID is `62547638`. GitHub reports
administrator bypass remains available under its default setting. Reviewed
production variables are saved in `infra/github/production-variables.env`.
The operator applied all seven variables with `gh variable set --env-file`, then
supplied `gh variable list` output matching the reviewed AWS resource outputs and
`FINBOT_ACTIVATION_APPROVED=false`. Read-only repository-variable inspection
confirmed `FINBOT_DELIVERY_ENABLED` remains unset. The operator published commit
`deba29f` with these changes. [CI run 38007413522](https://github.com/ilancooke/finbot-filings/actions/runs/38007413522)
failed in three offline capacity-replay cases (552 application tests passed,
eight container tests skipped); infrastructure, workflow lint and image checks
were not reached. Application delivery was skipped. The replay driver now waits
for actual worker wait boundaries instead of treating one millisecond of wall time
as completion. The test-only correction includes a slower-SDK regression case.
Local validation passed: 556 application tests, eight opt-in container cases
skipped, 19 infrastructure tests, compilation and whitespace checks. The operator
published the correction as `8d67228` and [CI run 38009224575](https://github.com/ilancooke/finbot-filings/actions/runs/38009224575)
succeeded, including application/infrastructure checks, workflow lint and ARM64
image/lifecycle validation. Its application-delivery run was skipped because
delivery remains disabled. Read-only GitHub inspection reconfirmed all seven
production variables and `FINBOT_ACTIVATION_APPROVED=false`. The repository-scope
enablement input is saved in `infra/github/repository-variables.env`. The operator
applied it and read-only GitHub inspection confirmed `FINBOT_DELIVERY_ENABLED=true`
at repository scope while production `FINBOT_ACTIVATION_APPROVED=false` remains.
The operator manually dispatched [staged release run 38010178736](https://github.com/ilancooke/finbot-filings/actions/runs/38010178736)
from `main` with `activate=false`. Read-only inspection confirmed the run targets
the tested commit `8d67228e260b969e3a737a87ed36b36e63568452` and initially waited
for the production environment's required reviewer. The operator approved
environment ID `23924540932` through the standard pending-deployments API,
creating GitHub deployment `6973914445`. The workflow completed successfully at
`2026-10-10T00:50:31Z` (2026-10-09 in the operator's timezone), including offline
application/container checks, OIDC credentials, image publication and staging.
Its retained release manifest reports `outcome=staged`, `checkpoint=verified`,
previous revision 1, new revision 2, previous count 0 and no observed tasks:

- Image tag: `8d67228e260b969e3a737a87ed36b36e63568452-38010178736-1`.
- Image digest: `sha256:1693bde7e53ff91af39668c7b115610572b43eedada23268c32c0e8cc12b2fb8`.
- Task definition: `arn:aws:ecs:us-east-1:559007813222:task-definition/finbot-prod-ingestion:2`.

Read-only AWS MCP inspection confirmed the service uses revision 2, its rollout
is `COMPLETED`, and desired/running/pending counts are all zero. The active task
definition specifies Linux ARM64 and exactly the manifest's ECR digest. The CDK
baseline remains revision 1; application delivery stages revisions separately.
The operator inspected this digest with `aws ecr describe-image-scan-findings`:
scan status is `COMPLETE` with one `HIGH` finding. The operator then supplied the
finding details, confirming `CVE-2026-85091` in zlib source package
`1.3.dfsg+really1.3.1-1`, CVSS 4 score `8.3`, matching the initial image's finding.
It remains open for this release as well. Continue stopped-service preparation;
recheck remediation or record an explicit operator decision before activation.
Company identity/seed preparation, live application validation and activation
were still pending at that checkpoint; staging does not demonstrate live
application readiness. The operator subsequently supplied a strongly consistent
Companies-table scan with `Count=0` and `ScannedCount=0`. The selected 50 symbols
now have an exact, unique SEC mapping and a reviewed-format transaction input in
`infra/seed/companies.prod.json`; see PRODUCTION_UNIVERSE.md for provenance and
the complete review table. The operator applied the initial seed transaction
successfully; its response reported `150.0` write capacity units. Read-only AWS
MCP verification scanned the base table with strong consistency and returned 50
records with no continuation key. All stored attributes match the input exactly,
with zero missing, extra or mismatched records. The operator then queried the
`EnabledCompanies` index and confirmed `Count=50`, `ScannedCount=50`. Company
seeding and index verification are complete; live calendar/application checks,
alarm routing and activation remain pending.
Offline seed validation passed 557 application tests (eight opt-in container
cases skipped), including the new seed safety/identity contract check. AWS SDK
request-shape validation and whitespace checks passed; no AWS client or write
was used during validation.

| Field | Meaning |
| --- | --- |
| account / region | Explicit 12-digit target account and AWS region |
| environment / prefix | Lowercase resource names; metrics Environment uses environment |
| sec_user_agent | Identifying application/contact address, no credentials |
| github_subject | Exact main-branch or protected-environment subject; copy the repository's actual prefix, including `OWNER@OWNER-ID/REPO@REPO-ID` when immutable subjects are enabled |
| image_digest | Real validated ARM64 ECR image digest, `sha256:...` |
| github_oidc_provider_arn | Optional existing account GitHub OIDC provider; import it instead of duplicating it |
| alarm_action_arn | Optional existing same-region/account SNS notification topic |
| alarm_email | Optional email subscription on a CDK-managed alarm SNS topic; mutually exclusive with alarm_action_arn; keep personal addresses in ignored config.local.json |
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

Read the actual subject prefix with this read-only command:

```bash
gh api repos/OWNER/REPO/actions/oidc/customization/sub
```

For default subjects, append `:environment:production` to the returned
`sub_claim_prefix` when using the workflow's production environment. Do not infer a
name-only prefix from `use_default=true`; immutable IDs can also be enabled.
The configuration accepts either format and emits an exact IAM `StringEquals`
condition. See [GitHub's OIDC reference](https://docs.github.com/en/actions/reference/security/oidc).

The default VPC uses two public subnets in region suffixes `a` and `b`, a task public
IP and HTTPS-only egress. Verify those AZs are supported in the target account.
There are no inbound rules, load balancer or ports. This avoids NAT for v0; private
subnets/egress require a reviewed infrastructure change, not an automatic fallback.
S3 uses SSE-S3/versioning; DynamoDB has on-demand capacity, AWS-owned encryption,
PITR and deletion protection. SNS message bodies are intentionally unencrypted
at rest, with HTTPS publication enforced. SQS uses SSE-SQS and a 14-day retention
period. [ADR 010](adr/010-use-service-managed-encryption-without-kms-integration.md)
records the accepted policy: no project KMS keys, aliases or application KMS
permissions. AWS services can still use KMS internally for AWS-owned encryption.

Bucket policies reject writes lacking exact If-None-Match `*`. Ingestion cannot
remove objects/versions. No multipart/copy bypass or raw-data expiration is
configured. Data tables, bucket, topic, queue and ECR are retained across
stack deletion/replacement; both stacks have termination protection. Do not rename
construct IDs or assume RETAIN makes resource replacement safe. Always review diff.
CloudWatch logs retain 30 days. ECR tags are immutable, scanning is requested on
push, and no lifecycle expiration can remove a release needed for rollback.

## Authorized first deployment

Infrastructure deployment is manual. Application workflows never run CDK deploy.
Keep repository-level `FINBOT_DELIVERY_ENABLED` unset/false initially.
Run the pinned local schema and encryption-policy checks described in
[infra/validation/README.md](../infra/validation/README.md) before deploying the
bootstrap, and again on the application templates synthesized from your actual
environment configuration. Review lint warnings; fixture validation does not
establish that a real account deployment will succeed.

1. Authenticate using the approved local AWS credential mechanism. Verify target
   account/region and authorize CDK bootstrap separately if needed.
2. Review the checked-in customized template and parameter policy in
   [infra/bootstrap/README.md](../infra/bootstrap/README.md). Bootstrap only after
   authorization, using the saved template and your administrative profile:

   ```bash
   npm run cdk -- bootstrap aws://ACCOUNT/REGION \
     --template infra/bootstrap/bootstrap-template.yaml \
     --profile default \
     --termination-protection \
     --cloudformation-execution-policies arn:aws:iam::aws:policy/AdministratorAccess
   ```

   This account/region stack is shared CDK deployment infrastructure, separate
   from Finbot's state/runtime stacks. The saved template derives from the pinned
   CLI and applies ADR 010's SSE-S3 policy with variant `Finbot: SSE-S3 v1`.
   Always pass `--template`; the upstream default does not express this policy.
3. Set the explicit local configuration; run strict synth and review
   `npm run cdk -- diff PREFIX-ENV-state --method template --profile default --no-lookups`.
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

The reviewed JSON inputs and standard GitHub CLI commands for creating the
production environment, requiring `ilancooke` approval and allowing only `main`
are saved in [infra/github](../infra/github/README.md). The operator has applied
the environment and branch rules, and applied and verified the seven non-secret
production variables. The same folder preserves these inputs and their standard
bulk CLI command for future updates. Code publication and CI verification have
succeeded. Delivery is enabled and the first staged release succeeded as recorded
above; the repository-scope enablement input is also saved in that folder.

Repository variable `FINBOT_DELIVERY_ENABLED=true` enables the release job and is
now applied. In the
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
remain pending. Do not set the approval flag or start production until the
readiness checks below are complete.

Before preparing company seed inputs, inspect whether the target Companies table
already contains records. This read-only request scans at most one item; `Count=1`
means records exist, not that the whole table contains exactly one record:

```bash
aws dynamodb scan \
  --table-name finbot-prod-companies \
  --select COUNT \
  --consistent-read \
  --limit 1 \
  --no-paginate \
  --profile default \
  --region us-east-1 \
  --output json \
  --no-cli-pager
```

After reviewing the mapping in [PRODUCTION_UNIVERSE.md](PRODUCTION_UNIVERSE.md),
the operator can authorize initial seeding by running the following command from
the package root. It makes a real data change: 50 enabled company records in the
target table, with no service activation. The checked-in JSON contains the entire
request, including the account/region-specific table ARN, schema version, enabled
index markers and a create-only condition for every CIK:

```bash
aws dynamodb transact-write-items \
  --cli-input-json file://infra/seed/companies.prod.json \
  --profile default \
  --region us-east-1 \
  --output json \
  --no-cli-pager
```

This single transaction either creates all 50 items or writes none. If any CIK
already exists, the transaction fails rather than replacing it. Preserve the
conditions when investigating errors; do not delete existing records to retry.
The fixed client request token permits identical retries for ten minutes; after
that window an already successful seed fails its create-only conditions. Keep
this initial input unchanged once applied; subsequent universe edits require
separately reviewed inputs. The returned capacity details are not a row count;
verify the actual stored identities and enabled index separately after success.
See the [AWS transaction API](https://docs.aws.amazon.com/amazondynamodb/latest/APIReference/API_TransactWriteItems.html)
and [CLI input documentation](https://docs.aws.amazon.com/cli/latest/reference/dynamodb/transact-write-items.html).

After comparing the base-table records with the reviewed input, verify the index
used by the runtime to enumerate enabled companies:

```bash
aws dynamodb query \
  --table-name finbot-prod-companies \
  --index-name EnabledCompanies \
  --key-condition-expression 'enabled_marker = :enabled' \
  --expression-attribute-values '{":enabled":{"S":"ENABLED"}}' \
  --select COUNT \
  --profile default \
  --region us-east-1 \
  --output json \
  --no-cli-pager
```

For this initial 50-record seed, expect `Count=50` and `ScannedCount=50`.
Global secondary indexes have eventual consistency; omit `--consistent-read`.
If checked immediately after writing, allow the index to catch up and repeat
this read-only query. A persistent mismatch requires inspection rather than
rerunning the seed. This index check does not start ingestion or establish
calendar/provider readiness. See the [Query CLI reference](https://docs.aws.amazon.com/cli/latest/reference/dynamodb/query.html).

### One-shot live calendar check

Company seeding and enabled-index verification are complete. Use the maintained
provider-only command to collect the full production calendar range while ECS
remains stopped:

```bash
.venv/bin/python -m finbot_ingestion.calendar.check \
  --companies-file infra/seed/companies.prod.json \
  --days 30 \
  --output /private/tmp/finbot-calendar-check-20261009.json
```

Preparation validation passed 567 application tests (eight opt-in container
cases skipped), compilation, entry-point help and whitespace checks. The new
offline cases cover all 30 fixture dates, scope rejection, visible missing
observations, AWS-client exclusion, sanitized failures, preservation of existing
reports and cancellation cleanup. No live Yahoo check was run during preparation.

The operator subsequently ran the command successfully on 2026-10-09 local time
(observed at `2026-10-10T01:22:55+00:00`). Collection completed for all 30 dates,
2026-10-09 through 2026-11-07, with 50 requested companies, 49 events and 49 matched
companies in 180.061 seconds. NVDA had no observation; the operator confirmed
that absence is expected for this window. Default collection bounds were used
and `aws_writes=false`. The report remains outside git at the path above. This
completes the operator-machine live collection and absence review; cloud runtime
validation and ongoing access readiness remain separate.

Running this command authorizes that bounded live Yahoo check. It reads the
already reviewed seed as company input, makes no AWS or SEC calls, never starts
the ingestion application and writes no calendar/checkpoint records. It uses
the same production Yahoo adapter, schema/total/pagination consistency checks,
30 inclusive America/New_York dates, request pacing and bounded retries. Default
bounds are 600 seconds, 300 HTTP attempts and 30,000 raw rows; `YAHOO_*` overrides
are reflected in the report. A dedicated worker owns a fresh private temporary
cache and closes the session/cache before returning. The check does not reuse
or persist authentication material in its report. `--help` is offline.

The report path must not already exist. Success returns exit zero and
`status=collection_complete`; errors return exit one, `collection_complete=false`
and only the exception type. The terminal summary excludes event details; the
local JSON includes normalized ticker/CIK/date/timing observations and the list
of companies with no observations. It excludes raw provider payloads, cookies,
authenticated URLs and exception messages. Preserve the report outside git and
use a new filename for subsequent checks.

Review the requested dates/company count, consistency result, matches and
unmatched symbols. A complete collection does not prove per-company coverage,
source accuracy, omitted-event cancellation or durable calendar synchronization.
No observation in a 30-day window can be legitimate; do not invent an event or
silently switch the selected ticker to obtain a match. Investigate unexpected
gaps before approving production readiness. A check from the operator's machine
does not verify Fargate networking or the deployed task's permissions.

Ongoing Yahoo access remains a separate operator decision. The existing adapter
uses keyless yfinance access; this command adds no credential/subscription setup.
The [upstream project](https://github.com/ranaroussi/yfinance) describes personal
research/educational use and directs users to Yahoo's terms for data-use rights.
A successful fetch is evidence of that check, not an access guarantee for future
daily refreshes or a license decision.

### Remaining activation checks

1. The initial complete 30-day scoped Yahoo collection and absence review passed
   as recorded above. Resolve the ongoing access arrangement. Set `calendar_provider` to `yahoo` in
   reviewed CDK context/JSON configuration. The runtime uses keyless pinned yfinance,
   private `/tmp/finbot-yahoo` caches, 30-day daily refresh and replacement-only
   reconciliation. The default placeholder cannot establish readiness. Offline
   fixture tests and the earlier partial live probe remain historical evidence.
2. Company setup is complete: the selected
   [50-symbol production universe](PRODUCTION_UNIVERSE.md) was mapped, seeded and
   verified against the base table and enabled index as recorded above. Do not
   rerun the initial create-only seed transaction as a readiness check.
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

Set `alarm_email` in ignored `infra/cdk/config.local.json` to provision an SNS
topic, email subscription and topic policy in the runtime stack. All 11 alarm
actions route to that topic. The policy allows `cloudwatch.amazonaws.com` to
publish only from this account's exact alarm ARNs and denies insecure transport.
The topic is retained and uses no KMS key, consistent with ADR 010. The new
`AlarmTopicArn` output identifies it. Alternatively, supply `alarm_action_arn`
for an existing topic whose owner must configure publishing and subscriptions.
Enabling monitoring without either destination fails configuration validation.

Keep `monitoring_enabled=false` while provisioning and confirming the email
subscription. SNS sends a confirmation email; the recipient must follow its
confirmation link before notifications are delivered. This endpoint confirmation
is required by SNS and cannot be replaced by declaring the subscription in CDK.
Confirm actual delivery before enabling alarm actions in a subsequent reviewed
runtime deployment. An intentionally stopped service breaches the liveness alarm;
alarm evaluation and ingestion activation remain independent.

Before either deployment, follow the manual-infrastructure serialization procedure
below: suspend delivery, preserve the service's current image digest in CDK inputs,
review the runtime diff and keep desired count zero. If the baseline task definition
changes, refresh the GitHub production baseline variable from the stack output.
Creating the topic and subscription alone does not activate ingestion.

Preparation status: the operator selected the recipient, saved only in ignored
local config. Read-only inspection reconfirmed the stopped service on revision 2
and its staged digest; the local CDK digest was updated to preserve that image.
Strict offline production synthesis passed with one email subscription and 11
routed alarms, actions disabled and desired count zero. The repository delivery
flag was subsequently set to false by the operator and verified read-only; recent
delivery runs are completed. The operator ran the runtime template diff and it
matches the planned notification changes: one SNS topic, scoped topic policy,
email subscription, all 11 alarm actions and the `AlarmTopicArn` output. The only
task-definition replacement changes the original baseline image to the currently
staged digest; task definitions are immutable and this creates a new revision.
No action-enable or desired-count change appeared. The operator deployed the
reviewed runtime update successfully. Read-only verification confirmed
`UPDATE_COMPLETE`, baseline/service revision `3`, completed rollout and zero
desired/running/pending tasks, with no listed running tasks. All 11 alarms route
to the new topic and have `ActionsEnabled=false`; the liveness alarm is in ALARM
because ingestion is intentionally stopped.

The deployed topic is
`arn:aws:sns:us-east-1:559007813222:finbot-prod-runtime-AlarmNotificationsA4AFC78C-PywcSrOpIKGt`.
The recipient confirmed the subscription email; read-only SNS inspection now
returns a subscription ARN instead of `PendingConfirmation`. The operator issued
the test publish below and confirmed the email arrived; SNS-to-email delivery is
verified. Reproduction command:

```bash
aws sns publish \
  --topic-arn arn:aws:sns:us-east-1:559007813222:finbot-prod-runtime-AlarmNotificationsA4AFC78C-PywcSrOpIKGt \
  --subject 'Finbot alarm notification test' \
  --message 'Finbot SNS delivery test. Ingestion remains stopped and alarm actions are disabled.' \
  --profile default \
  --region us-east-1 \
  --output json \
  --no-cli-pager
```

A returned `MessageId` establishes that SNS accepted the publish; confirm receipt
in the destination inbox to establish SNS-to-email delivery. This operator publish
does not trigger a CloudWatch alarm or validate CloudWatch's service-principal
publishing path. It changes no ingestion or alarm action settings. See the
[SNS CLI publishing guide](https://docs.aws.amazon.com/cli/v1/userguide/cli-services-sns.html).

The checked-in production variables
file now references baseline revision `3`; applying it to GitHub remains pending.
Read-only GitHub inspection confirmed the environment still references revision
`1`; its other six saved variables already match the file. Reapply the reviewed
production variable file to update the baseline:

```bash
gh variable set \
  --repo ilancooke/finbot-filings \
  --env production \
  --env-file infra/github/production-variables.env
```

The operator reapplied the file successfully. Read-only GitHub verification
confirmed all seven production variables match it, including baseline revision
`3` and `FINBOT_ACTIVATION_APPROVED=false`. Repository
`FINBOT_DELIVERY_ENABLED=false` is also confirmed. Next, commit and push the
prepared seed, diagnostic, notification definitions and deployment records for
CI validation while delivery remains paused. Local validation passed 567
application tests (eight opt-in container cases skipped), 26 infrastructure/
workflow tests, compilation, strict offline production synthesis and whitespace
checks; the pushed commit's CI result remains pending.

References: [SNS alarm publishing permissions](https://repost.aws/knowledge-center/cloudwatch-receive-sns-for-alarm-trigger)
and [SNS subscription confirmation](https://docs.aws.amazon.com/sns/latest/dg/sns-access-policy-use-cases.html).

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
