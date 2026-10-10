# CDK account/region bootstrap

`bootstrap-template.yaml` is a Finbot customization of the standard bootstrap
export from **aws-cdk 2.1145.0**, pinned in `package.json` and `package-lock.json`.
It retains bootstrap resource version **32** and uses variant **Finbot: SSE-S3 v1**.
The upstream Apache-2.0 license and notice are preserved in `LICENSE` and `NOTICE`.

The `CDKToolkit` CloudFormation stack provides deployment asset storage (S3 and
ECR), five deployment/publishing/lookup/execution roles, policies and an SSM
version parameter. It is shared by CDK applications in the same account/region,
including future Finbot packages. Application state/runtime stacks live in
`../cdk/`; application image delivery does not deploy bootstrap.

## Encryption decision and compatibility

[ADR 010](../../docs/adr/010-use-service-managed-encryption-without-kms-integration.md)
selects SSE-S3 for bootstrap assets. Compared with the pinned upstream template:

- The bucket explicitly uses `AES256` encryption. Private access, versioning,
  HTTPS enforcement, lifecycle behavior and retention remain unchanged.
- Conditional KMS key/alias resources, their conditions, file-publishing KMS
  permissions, cross-account pipeline KMS permissions and the deprecated
  `FileAssetKeyArn` output/export are removed.
- `BootstrapVariant` identifies this customization so a normal bootstrap with
  the upstream variant will not silently replace it.
- Required bucket/domain/repository/version outputs, SSM version, five roles,
  their trust relationships, role names and default qualifier remain unchanged.
- The existing lookup-role **deny** on `kms:Decrypt` remains a security guard.
  It grants no access and creates no encryption dependency.
- `FileAssetsBucketKmsKeyId` is an inert compatibility parameter: CDK 2.1145.0
  supplies it even for custom templates. Only `AWS_MANAGED_KEY` is accepted;
  it is deliberately unused and never selects encryption. The bucket
  always uses SSE-S3. Do not pass customer-key options.

AWS-owned encryption internal to other services is compatible with ADR 010;
Finbot does not manage keys or call KMS. The standard CloudFormation administrative
execution policy below is retained for deployment and is not an application key grant.

## Review and deploy

Deployment record (2026-10-09): the operator ran AWS `validate-template`
successfully, then reported successful bootstrap of `aws://559007813222/us-east-1`
with the command below using the saved template, `default` profile, termination
protection and administrative execution policy. Operator-supplied read-only output
confirmed `CREATE_COMPLETE`, termination protection enabled, bootstrap version `32`,
bucket `cdk-hnb659fds-assets-559007813222-us-east-1` (and its regional S3 domain),
and repository `cdk-hnb659fds-container-assets-559007813222-us-east-1`.
The operator subsequently deployed both application stacks after image publication
and template review. Operator-supplied read-only ECS output confirmed zero desired,
running and pending tasks; production activation remains pending. See
[DEPLOYMENT.md](../../docs/DEPLOYMENT.md) for outputs, image assessment and remaining
verification/delivery/activation steps.

Run commands from the repository root. Install the locked CLI with
`npm ci --ignore-scripts --no-audit --no-fund` if necessary. Review the saved template,
confirm the target account/region and use your administrative profile.

The following command **creates or updates real AWS resources**. Replace
`ACCOUNT` and `REGION` with the explicitly authorized deployment target.

```bash
npm run cdk -- bootstrap aws://ACCOUNT/REGION \
  --template infra/bootstrap/bootstrap-template.yaml \
  --profile default \
  --termination-protection \
  --cloudformation-execution-policies arn:aws:iam::aws:policy/AdministratorAccess
```

Always supply `--template`: the upstream default does not express this policy.
Stack name `CDKToolkit` and qualifier `hnb659fds` retain CDK defaults. No additional
accounts are trusted. Termination protection prevents accidental stack deletion;
keep bootstrap while applications depend on it and update it in place.
CloudFormation's execution role receives `AdministratorAccess`, the standard CDK
deployment default. Application task and GitHub release permissions remain scoped.
Successful bootstrap prepares the environment; continue with
[DEPLOYMENT.md](../../docs/DEPLOYMENT.md) for application stacks and activation.

This template is intended for the first deployment. If a standard bootstrap
already exists, review variant differences, existing key exports/imports and
access to old encrypted assets before migration. Do not use `--force` as an
unreviewed shortcut or delete keys/retained resources as incidental cleanup.

## Review upstream updates

Export the pinned upstream template locally to compare with our customized file:

```bash
node_modules/.bin/cdk bootstrap --show-template --no-notices \
  > /tmp/finbot-bootstrap-upstream.yaml
diff -u /tmp/finbot-bootstrap-upstream.yaml infra/bootstrap/bootstrap-template.yaml
```

Differences are expected and listed above. This export does not contact AWS or
deploy. On CLI upgrades, review upstream changes, reapply the documented policy
in the saved YAML, update versions here, and run infrastructure tests and strict
synthesis. Never overwrite the customized file directly with an upstream export.
Keep the variant stable for compatible updates; the bootstrap resource version
must satisfy the CDK synthesizer. CLI upgrades do not update deployed resources.

Run the pinned local schema and encryption-policy checks in
[infra/validation](../validation/README.md) before deployment or template updates.
That document records the current reviewed lint warnings and validation scope.

References: [AWS bootstrap customization](https://docs.aws.amazon.com/cdk/v2/guide/bootstrapping-customizing.html)
and [bootstrap CLI options](https://docs.aws.amazon.com/cdk/v2/guide/ref-cli-cmd-bootstrap.html).
