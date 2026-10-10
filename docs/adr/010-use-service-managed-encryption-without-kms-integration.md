# ADR 010: Use service-managed encryption without application KMS integration

- Status: Accepted for implementation; deployment remains separately authorized
- Date: 2026-10-09
- Supersedes: Phase 8 dedicated SNS key and AWS-managed DynamoDB key choices;
  the initial standard bootstrap asset-encryption choice

## Context

Finbot acquires public SEC documents and publishes metadata-only artifact events.
The current scope has no customer-managed-key, key-level audit, cross-account
decryption or compliance requirement. The user accepts SNS message bodies being
unencrypted at rest and prefers to avoid KMS-specific permissions, operations and
charges. Stored data can use encryption managed internally by each service.

## Decision

This is the repository-wide policy for `finbot-filings`, including its bootstrap
and application infrastructure:

- Use **SSE-S3** (`AES256`) for the CDK bootstrap asset bucket and SEC artifact
  bucket. Do not select SSE-KMS or provision a bucket encryption key.
- Use DynamoDB's default **AWS-owned key** (`TableEncryption.DEFAULT`). Tables,
  indexes and backups remain encrypted; this does not select an AWS-managed
  account key or require application KMS permissions.
- Keep **SSE-SQS** for operational failed-work messages.
- Leave the `ArtifactReady` SNS topic **unencrypted at rest** by omitting
  `KmsMasterKeyId`. Preserve HTTPS publication and scoped `sns:Publish` permission.
- Use service defaults for ECR and CloudWatch Logs encryption. Do not add custom
  keys, aliases, key identifiers, KMS clients or KMS-specific Allow statements
  for these application services. Future additions should follow this policy
  unless an explicitly approved requirement changes it.
- Preserve HTTPS transport, private storage, immutable-write enforcement, data
  retention, DynamoDB recovery protection and existing role separation.

AWS services can use KMS internally, including DynamoDB's AWS-owned encryption.
The decision avoids project-managed KMS integration; it does not claim that AWS
internally uses no KMS. The standard bootstrap lookup-role deny on `kms:Decrypt`
remains as a protective guard, and the existing CloudFormation administrative
deployment policy is unchanged.

The bootstrap template is a version-controlled customization of pinned CDK
2.1145.0, bootstrap resource version 32, using variant `Finbot: SSE-S3 v1`.
Preserve the five bootstrap roles, naming/qualifier and required outputs/version
contract. Remove conditional KMS resources, KMS-specific Allow statements and
the deprecated key-ARN output. Retain the inert `FileAssetsBucketKmsKeyId`
compatibility parameter because the pinned CLI supplies it; it cannot switch
the bucket away from SSE-S3. Use the saved template for all bootstrap operations.

## Consequences

The infrastructure has fewer resources and permission dependencies. Acquisition,
immutable history, event contents and at-least-once delivery remain unchanged.
SNS message bodies have no topic-level encryption at rest, as explicitly accepted;
the application must continue excluding credentials and document contents from events.
Service-managed storage encryption remains enabled.

This decision is implemented before first deployment. It does not authorize AWS
changes or deletion of existing retained keys. Applying it to an already deployed
environment requires reviewing exports/imports, retained-key ownership and old
encrypted-data access; keys must not be deleted as an incidental cleanup step.
Customized bootstrap templates must be reviewed against upstream on CDK upgrades.

## Revisit when

Sensitive private data, customer or regulatory requirements, cross-account key
control, customer-managed-key auditing, or independently controlled decryption
becomes part of the project scope. Any change requires a new approved decision.

## References

- [S3-managed encryption](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingServerSideEncryption.html)
- [DynamoDB encryption options](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/EncryptionAtRest.html)
- [SNS encryption scope and KMS](https://docs.aws.amazon.com/sns/latest/dg/sns-server-side-encryption.html)
- [Customizing CDK bootstrap](https://docs.aws.amazon.com/cdk/v2/guide/bootstrapping-customizing.html)
