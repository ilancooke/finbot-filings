# GitHub delivery configuration

These configuration files are the reviewed GitHub setup inputs for the
`ilancooke/finbot-filings` production environment. Apply them from the package root
using the standard GitHub CLI; no custom script or console setup is required.
These inputs are not CloudFormation resources and are applied separately from CDK.

`production-environment.json` removes required reviewers and the wait timer while
selecting custom deployment branch rules. `production-branch-policy.json` allows
only branch `main`, with no tags. The scoped AWS OIDC trust still requires this
production environment. After setup, a direct push to `main` runs validation and
automatic application delivery in `ci.yml`; no per-release approval is required.
Initial production readiness and explicit activation remain separate decisions.

## Upgrade an existing deployment

These are one-time setup steps, not the routine release process:

1. Before publishing the consolidated workflow, pause delivery with the repository
   flag below and verify no old release is running or waiting for approval. Let
   active handoffs finish; cancel obsolete approval-waiting jobs. Keep the service
   running. This avoids overlapping an old workflow with the replacement.
2. Publish the reviewed changes directly to `main` and require the new `CI` run to
   pass. The paused flag skips its release job; it does not stop ingestion.
3. Apply the environment PUT below to remove the old required reviewer. Verify the
   existing branch policy still permits only branch `main`; do not POST a duplicate.
   Verify production variables and OIDC target settings; preserve the approved
   activation flag and current baseline ARN. No AWS/CDK change is required.
4. Set the repository delivery flag to `true` and dispatch `ci.yml` from current
   `main` with `activate=false`. This replaces a running worker automatically or
   stages a stopped service. Inspect the run and retained release manifest.
5. Leave delivery enabled. Subsequent pushes to `main` validate and deploy without
   manual toggles, approvals or service shutdown commands.

Live GitHub settings are not changed by committing these files. The one-time
configuration rollout and first release must be authorized separately from local
implementation. Existing intentionally paused services remain stopped.

## Initial setup

Authenticate with `gh auth login` before applying these files. First create or
update the environment and its protection settings:

```bash
gh api --method PUT \
  repos/ilancooke/finbot-filings/environments/production \
  --input infra/github/production-environment.json
```

Then create the main-branch deployment rule:

```bash
gh api --method POST \
  repos/ilancooke/finbot-filings/environments/production/deployment-branch-policies \
  --input infra/github/production-branch-policy.json
```

The environment PUT is repeatable. The branch-policy POST is for initial creation;
on subsequent changes, list policies and update the existing policy by ID instead
of creating duplicates. Verify both settings with read-only requests:

```bash
gh api repos/ilancooke/finbot-filings/environments/production \
  --jq '{name,protection_rules,deployment_branch_policy}'
gh api repos/ilancooke/finbot-filings/environments/production/deployment-branch-policies
```

The production environment/branch files do not enable delivery or start ECS tasks. Keep
`FINBOT_DELIVERY_ENABLED` unset/false and `FINBOT_ACTIVATION_APPROVED` unset/false
until the applicable steps in [DEPLOYMENT.md](../../docs/DEPLOYMENT.md) are complete.
Code publication is a separate setup step.

## Production variables

`production-variables.env` contains seven non-secret environment variables copied
from the operator's reviewed CloudFormation outputs. It includes the region,
release role, ECS cluster/service, ECR repository, CDK baseline task definition,
and `FINBOT_ACTIVATION_APPROVED`. The saved activation flag is `true` following
completed readiness/activation. Applying variables does not itself start ingestion;
review their intended values for the target environment before use.
The filename is a dotenv input format for
the CLI, not an application credentials file. It is intended for version control.
Do not add AWS access keys or SEC contact details here.

Load all seven variables with one standard CLI command:

```bash
gh variable set \
  --repo ilancooke/finbot-filings \
  --env production \
  --env-file infra/github/production-variables.env
```

Verify their values:

```bash
gh variable list --repo ilancooke/finbot-filings --env production
```

The bulk command creates or updates variables; it does not remove unrelated
variables. If CDK changes the baseline task definition or replaces a role, review
the updated stack outputs, update this file, and reapply it before the next release.
Do not include `FINBOT_DELIVERY_ENABLED` in this environment file: the workflow's
job condition reads that flag at repository scope. Keep it unset/false until code
publication, CI and delivery target verification are complete. Applying these
variables alone does not run a workflow or activate ingestion.

## Enable automatic delivery

After target settings and production readiness are verified, apply the repository
flag at repository scope (omit `--env`):

```bash
gh variable set \
  --repo ilancooke/finbot-filings \
  --env-file infra/github/repository-variables.env
```

This sets `FINBOT_DELIVERY_ENABLED=true`. Leave it enabled for daily work. A push
to `main` runs the one workflow's checks and ARM64 lifecycle tests, then its
serialized production job publishes the tested image and calls `release.py`.
It never rebuilds the deployment image. Each release checks current main before
publication and again before the first ECS mutation; superseded commits skip.
Active handoffs finish or restore without cancellation. Inspect the retained
`release-manifest.json` artifact for the result and exact digest/revisions.

Changing the flag does not rerun an already skipped job. Dispatch the current
`main` to retry validation/delivery or perform the first post-setup release:

```bash
gh workflow run ci.yml \
  --repo ilancooke/finbot-filings \
  --ref main \
  -f activate=false
```

`activate=false` preserves a running service's desired count one and a paused
service's desired count zero. It does not stop a running service for maintenance.
Initial activation additionally requires `FINBOT_ACTIVATION_APPROVED=true` and an
explicit dispatch with `activate=true`. Keep that environment flag false until
readiness is complete. The checked-in true value records the existing production
readiness decision; do not apply it blindly to a new environment.

To activate or roll back using a previously validated compatible ARM64 image,
review the digest and dispatch from current `main`:

```bash
gh workflow run ci.yml \
  --repo ilancooke/finbot-filings \
  --ref main \
  -f activate=true \
  -f image_digest=sha256:REVIEWED_DIGEST
```

The same validation gate runs; the selected existing image is pulled and checked
rather than rebuilt. Use `activate=false` when a paused service should stay paused.
Inspect results with `gh run view RUN_ID --repo ilancooke/finbot-filings`.

For emergency pause or infrastructure/data maintenance, disable future releases:

```bash
gh variable set FINBOT_DELIVERY_ENABLED \
  --repo ilancooke/finbot-filings --body false
```

The flag does not cancel an eligible, queued or running release and does not stop
ingestion. Verify release jobs have finished before manual maintenance. Restore
true afterward; normal application changes do not require this procedure.

The deployed AWS role must trust the exact repository subject returned by GitHub,
including immutable owner/repository IDs. See the runbook's OIDC verification;
do not change GitHub's subject format to accommodate an outdated IAM trust policy.

References: [environment API](https://docs.github.com/en/rest/deployments/environments#create-or-update-an-environment),
[branch-policy API](https://docs.github.com/en/rest/deployments/branch-policies#create-a-deployment-branch-policy),
[`gh api --input`](https://cli.github.com/manual/gh_api),
[`gh variable set --env-file`](https://cli.github.com/manual/gh_variable_set),
[`gh workflow run`](https://cli.github.com/manual/gh_workflow_run),
and [workflow concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).

## FilingReady topic cutover

The producer now targets `finbot-prod-filing-ready` through the CDK-managed
`FILING_READY_TOPIC_ARN` task setting. This release requires manual state/runtime
CDK deployment and a new baseline ARN before active application delivery.
Pause/stop first, stage the compatible new digest at zero, reconcile both stacks
and saved GitHub baseline variables, then explicitly activate. See the exact
[FilingReady rollout](../../docs/DEPLOYMENT.md#filingready-rollout). An old baseline
or old application image is not a compatible active combination. Keep the legacy
topic/export intact during cutover; subscriptions are not transferred automatically.

## Checkpoint compatibility

Filtering is delivered. For future incompatible schema/configuration changes,
follow [filtering compatibility](../../docs/DEPLOYMENT.md#filtering-compatibility):
pause delivery, stop/wait, stage at zero, reconcile CDK to the verified image/config
and update the baseline variable before activation. Historical pre-filter images
cannot read skip checkpoints and are unsuitable active rollback targets.
