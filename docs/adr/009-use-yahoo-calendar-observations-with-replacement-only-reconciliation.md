# ADR 009: Yahoo calendar observations with replacement-only reconciliation

- Status: Accepted for implementation; live operation is separately scoped
- Date: 2026-10-09

## Context

The agreed Yahoo handoff uses daily 30-day calendar observations as scheduling
hints. Collection of all reported rows does not prove that an absent expectation
was cancelled. Yahoo supplies no stable event ID or authoritative update timestamp.
The existing complete-snapshot reconciliation cancels missing in-scope records.

## Decision

Separate collection completeness from reconciliation authority. Yahoo uses an
explicit replacement-only policy; existing authoritative-snapshot providers keep
their omission-based policy. Incomplete collection never mutates expectations or
advances successful freshness.

Use CIK identity and market-local dates. Match a move only using unique, identical
versioned quarterly-announcement title evidence for the same provider/CIK. This
evidence is not a provider event ID. Unrecognized titles, multiple candidates and
simultaneously reported dates preserve expectations. Persist and strongly confirm
the replacement before cancelling the unchanged old observation. Retain tombstones.

Use a pinned yfinance authentication/transport seam to retain raw query/schema/
total evidence without depending on DataFrame conversion. Bound requests, pages,
rows, retries and elapsed time; repeat day slices to check source consistency.
Yahoo has its own executor, session, pacing and private temporary caches.

## Consequences

Source omissions/ambiguities can cause extra polling until existing window/grace
expiry. Strict consistency checks can reject a refresh during source changes;
existing expectations and safety polling remain available. Repeated reads do not
establish snapshot isolation or complete company coverage. Replacement writes are
not a multi-row transaction; interrupted work is repaired by a later valid refresh.

The existing Calendar table/key and satisfaction identities remain unchanged;
optional replacement evidence is additive. Older writers must not run concurrently
and discard the new evidence. Yahoo null IDs mean satisfaction is date-specific.
No cloud provisioning, universe seeding or live activation accompanies this change.
