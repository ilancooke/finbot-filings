# GitHub delivery configuration

These configuration files are the reviewed GitHub setup inputs for the
`ilancooke/finbot-filings` production environment. Apply them from the package root
using the standard GitHub CLI; no custom script or console setup is required.
These inputs are not CloudFormation resources and are applied separately from CDK.

`production-environment.json` names `ilancooke` (verified GitHub user ID `8453151`)
as the required deployment reviewer and selects custom deployment branch rules.
Self-review is allowed so the sole operator can approve their own workflow runs.
`production-branch-policy.json` allows the branch `main`; it does not allow tags.
After delivery is enabled, production release jobs wait for the operator's approval.
This approval does not replace the separate production-readiness/activation decision.

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
and `FINBOT_ACTIVATION_APPROVED=false`. The filename is a dotenv input format for
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

## Enable staged delivery after CI passes

The production environment and its targets must be verified, and the intended
main-branch commit must pass all CI checks, including workflow lint and ARM64
container/lifecycle tests. Then apply `repository-variables.env` at repository
scope (omit `--env`):

```bash
gh variable set \
  --repo ilancooke/finbot-filings \
  --env-file infra/github/repository-variables.env
```

This file sets `FINBOT_DELIVERY_ENABLED=true`. Future successful main-push CI runs
can create production release jobs that wait for reviewer approval. Changing this
variable does not rerun an already skipped delivery job. The first staged release
can be dispatched manually from `main` with `activate=false` after verifying the
flag:

```bash
gh workflow run deploy.yml \
  --repo ilancooke/finbot-filings \
  --ref main \
  -f activate=false
```

The job first waits for the production reviewer. After approval, it runs offline
application/image tests, authenticates to AWS using OIDC, publishes the ARM64
image and stages its ECS revision. Inspect the selected commit and pending job
before approval. Keep the production environment's `FINBOT_ACTIVATION_APPROVED=false` while
production-readiness work remains. A staged release to a stopped service publishes
an image and updates its ECS task revision while retaining desired count zero.
This does not authorize production activation or resolve the open image finding.

Inspect a dispatched run and its pending environment review using its run ID:

```bash
gh run view RUN_ID --repo ilancooke/finbot-filings
gh api repos/ilancooke/finbot-filings/actions/runs/RUN_ID/pending_deployments
```

After checking the selected commit and activation input, the required reviewer
can approve the staged release through the standard API. Replace `RUN_ID` with
the inspected run ID and confirm the pending response's production environment
ID matches `23924540932`:

```bash
gh api --method POST \
  repos/ilancooke/finbot-filings/actions/runs/RUN_ID/pending_deployments \
  -F 'environment_ids[]=23924540932' \
  -f state=approved \
  -f comment='Approve staged release with ingestion stopped (activate=false).'
```

This approval applies to that run only. The environment ID is a GitHub resource
identifier, not an AWS resource or credential. If the environment is recreated,
use the ID returned by the pending-deployments response instead.

To disable future delivery jobs, set the repository variable back to `false`:

```bash
gh variable set FINBOT_DELIVERY_ENABLED \
  --repo ilancooke/finbot-filings --body false
```

Disabling the flag does not cancel an in-progress release or stop an active service.

The deployed AWS role must trust the exact repository subject returned by GitHub,
including immutable owner/repository IDs. See the runbook's OIDC verification;
do not change GitHub's subject format to accommodate an outdated IAM trust policy.

References: [environment API](https://docs.github.com/en/rest/deployments/environments#create-or-update-an-environment),
[branch-policy API](https://docs.github.com/en/rest/deployments/branch-policies#create-a-deployment-branch-policy),
[`gh api --input`](https://cli.github.com/manual/gh_api),
and [`gh variable set --env-file`](https://cli.github.com/manual/gh_variable_set).
Manual dispatch uses [`gh workflow run`](https://cli.github.com/manual/gh_workflow_run).
Review uses the [pending-deployments API](https://docs.github.com/en/rest/actions/workflow-runs#review-pending-deployments-for-a-workflow-run).
