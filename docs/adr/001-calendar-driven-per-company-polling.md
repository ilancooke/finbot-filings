# ADR 001: Use calendar-driven per-company SEC polling in v0

- Status: Accepted

## Context

The initial supported universe is approximately 500 companies. The system needs near-real-time discovery around earnings releases while remaining simple enough to build, test, and explain as a portfolio project.

Two primary approaches were considered:

1. poll each company's SEC submissions endpoint;
2. ingest a global EDGAR latest-filings feed and filter locally.

An earnings calendar is available as a scheduling hint, so only a subset of the universe needs aggressive polling at any given time.

## Decision

v0 will use **calendar-driven per-company SEC polling**.

- One free earnings-calendar provider will be used behind a provider interface.
- Companies inside expected earnings windows will be polled approximately every 5–10 seconds, subject to the global SEC request budget.
- Companies outside active windows receive only low-frequency safety polling.
- Calendar data is not authoritative; SEC filings remain the source of truth.

## Consequences

### Positive

- Simple implementation and mental model.
- Easy to test and benchmark.
- Directly demonstrates calendar-driven scheduling.
- Keeps request volume manageable at ~500 companies.

### Negative

- Request volume grows with coverage.
- A wrong/missing earnings date can delay aggressive polling.
- Less efficient than global ingestion at very large scale.

## Revisit when

- coverage grows into the thousands;
- active reporters routinely saturate the SEC request budget;
- discovery latency degrades due to queueing;
- global latest-filings ingestion becomes operationally simpler than per-company polling.
