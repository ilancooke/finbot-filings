# ADR 003: Use S3 for raw artifacts and DynamoDB for metadata/state

- Status: Accepted

## Context

The service needs to preserve immutable SEC documents and maintain simple operational metadata/checkpoints. Expected access patterns are primarily key-based and state-oriented; relational joins are not required.

## Decision

- Store immutable raw filing documents/exhibits in **Amazon S3**.
- Store companies, calendar records, filing metadata, artifact metadata, durable checkpoints, and failure metadata in **DynamoDB**.
- Use deterministic S3 keys based on CIK, accession number, and filename.
- Do not compute/store content hashes in v0.
- Encryption follows [ADR 010](010-use-service-managed-encryption-without-kms-integration.md):
  SSE-S3 artifacts and AWS-owned DynamoDB encryption, with no project KMS integration.

## Consequences

### Positive

- S3 is a natural durable object store for raw documents.
- DynamoDB fits key/state access patterns and avoids unnecessary relational infrastructure.
- Clear separation between raw bytes and operational metadata.
- Restart logic can infer incomplete work from durable fields such as `stored_at` and `published_at`.

### Negative

- Ad hoc relational analytics are less convenient.
- Future access patterns may require GSIs or another analytical store.
- No content-hash validation/deduplication in v0.

## Revisit when

- relational queries become a first-class requirement;
- artifact integrity requirements justify hashing;
- large-scale analytics require a separate analytical system.
