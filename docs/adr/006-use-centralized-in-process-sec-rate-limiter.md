# ADR 006: Use one centralized in-process SEC request queue and rate limiter in v0

- Status: Accepted

## Context

Multiple companies may be due for polling at the same time, and newly discovered filings can trigger multiple document downloads. The service needs one global SEC request ceiling rather than an independent limit per ticker.

v0 runs a single ECS task.

## Decision

- Use an in-memory `asyncio.Queue` (or equivalent) for SEC work.
- Route all SEC HTTP calls through one shared client/rate limiter.
- Enforce a maximum of **5 SEC requests per second** across the entire ingestion task.
- Scheduler due times determine eligibility; the limiter determines actual send time.

## Consequences

### Positive

- Simple global enforcement in a single process.
- No distributed coordination infrastructure.
- Naturally interleaves polling across active companies.

### Negative

- Not safe as a global limiter if multiple ingestion tasks are introduced.
- Heavy artifact downloading can compete with polling for the same request budget.

## Revisit when

- more than one ingestion task is deployed;
- fairness/starvation becomes measurable;
- request volume requires distributed coordination.
