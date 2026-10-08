# ADR 002: Run the v0 ingestion service on ECS/Fargate

- Status: Accepted

## Context

The ingestion service is a continuously running workload that maintains an in-memory schedule, an internal request queue, and a centralized SEC rate limiter. The project is also intended to demonstrate cloud deployment and software engineering practices.

Alternatives considered included:

- homelab deployment;
- Lambda/EventBridge orchestration;
- ECS/Fargate.

## Decision

Deploy v0 as one continuously running **ECS/Fargate task**.

The same application remains runnable locally in Docker for development.

## Consequences

### Positive

- Natural fit for a long-running polling service.
- Preserves in-memory scheduling and rate limiting.
- ECS restarts failed tasks automatically.
- Demonstrates containerized AWS deployment.
- Easy path to future multi-task scaling if needed.

### Negative

- Higher baseline runtime cost than a purely event-driven serverless design.
- One task is a deliberate v0 availability/throughput limitation.

## Revisit when

- the workload becomes naturally schedule/event driven enough for Lambda;
- multi-task availability or throughput is required;
- operational cost becomes disproportionate to usage.
