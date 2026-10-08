# ADR 005: Preserve immutable publication history for point-in-time correctness

- Status: Accepted

## Context

SEC filings can be amended and companies can restate prior financial results. Downstream consumers may compute historical features such as P/E ratios "as known at the time."

If a restatement overwrote an earlier filing/result, historical backtests and point-in-time feature computations would incorrectly use information that was not available then.

## Decision

Treat SEC filings and raw artifacts as immutable historical facts.

- A new amendment/restatement is stored as a new filing under its own accession number.
- Original filings/artifacts are never overwritten to reflect later information.
- Preserve publication timestamps so downstream consumers can query the information set that existed as of a historical timestamp.

## Consequences

### Positive

- Supports point-in-time-correct features and backtests.
- Provides a complete audit/history trail.
- Avoids look-ahead bias introduced by later restatements.

### Negative

- Downstream consumers must explicitly choose whether they want latest-restated or point-in-time values.
- Storage/history is append-only rather than simplified to one current record.

## Revisit when

This is considered a foundational data requirement and should not normally be reversed.
