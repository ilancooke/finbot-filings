# ADR 004: Publish ArtifactReady through SNS with per-consumer SQS queues

- Status: Accepted

## Context

Stored SEC artifacts will have multiple downstream consumers with different urgency and processing behavior.

Examples:

- earnings extraction should begin immediately;
- RAG/indexing may be less time sensitive or processed in batches;
- future consumers may be added without changing ingestion logic.

A single shared SQS queue would cause consumers to compete for messages instead of independently receiving each artifact event.

## Decision

The ingestion service publishes one generic `ArtifactReady` event to an **SNS topic** after durable storage succeeds.

Each downstream service owns its own **SQS queue** subscribed to the topic.

## Consequences

### Positive

- Classic fanout semantics.
- Every consumer can receive every relevant artifact event.
- Each consumer has independent retry/backlog behavior.
- New consumers can be added without changing ingestion.
- SNS subscription filters may later reduce unnecessary deliveries.

### Negative

- More AWS resources than a single shared queue.
- Event schema must be kept stable/versioned.

## Revisit when

- routing complexity warrants EventBridge;
- cross-account/event-bus use cases emerge;
- event volume or filtering needs exceed the simplicity of SNS subscriptions.
