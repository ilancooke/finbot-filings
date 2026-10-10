# GitHub production environment

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

These files do not enable delivery, add AWS credentials or start ECS tasks. Keep
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

The deployed AWS role must trust the exact repository subject returned by GitHub,
including immutable owner/repository IDs. See the runbook's OIDC verification;
do not change GitHub's subject format to accommodate an outdated IAM trust policy.

References: [environment API](https://docs.github.com/en/rest/deployments/environments#create-or-update-an-environment),
[branch-policy API](https://docs.github.com/en/rest/deployments/branch-policies#create-a-deployment-branch-policy),
[`gh api --input`](https://cli.github.com/manual/gh_api),
and [`gh variable set --env-file`](https://cli.github.com/manual/gh_variable_set).
