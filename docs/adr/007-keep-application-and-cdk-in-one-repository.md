# ADR 007: Keep application code and CDK infrastructure in the same repository for v0

- Status: Accepted

## Context

The project is maintained by one developer and the AWS infrastructure is tightly coupled to one ingestion application. Separate infrastructure repositories are useful when platform ownership, permissions, or shared infrastructure require independent lifecycles, but those conditions do not exist yet.

## Decision

Keep AWS CDK code under `infra/cdk/` in the same repository as the application.

Application deployment and infrastructure deployment remain logically separate:

- GitHub Actions automatically tests/builds/deploys application images;
- CDK infrastructure changes are deployed manually at first.

## Consequences

### Positive

- Reviewers can see application and deployment definitions together.
- Simple local developer workflow.
- Less repository/process overhead.

### Negative

- Repo grows if many services later share infrastructure.
- Infrastructure permissions/review are not independently separated.

## Revisit when

- multiple services share platform infrastructure;
- a separate platform/infra ownership model emerges;
- infrastructure requires different access controls or release cadence.
