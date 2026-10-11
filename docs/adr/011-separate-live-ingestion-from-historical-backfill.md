# ADR 011: Separate live ingestion from historical backfill

- Status: Accepted; live cutoff implemented in source, rollout pending; backfill tooling deferred
- Date: 2026-10-10
- Supersedes: Incidental acquisition of historical `filings.recent` rows during
  normal live discovery

## Context

The deployed service parses every supported row in SEC's `filings.recent`
response. Previously unseen accessions become acquisition work regardless of
acceptance date. Initial startup therefore downloaded older filings, including
Alcoa's September 11, 2026 transaction-related 8-K and earlier annual reports.
The earnings calendar changes polling cadence; it does not bound acquisition age.

The user wants historical backfill to be a separate, deliberate operation. Live
earnings monitoring must not initiate historical collection simply because a
company or service has no existing DynamoDB filing records.

## Decision

- Normal live ingestion and historical backfill are distinct operations.
- Live acquisition uses a fixed UTC acceptance-time cutoff saved in configuration.
  Restarting or deploying the service must not reset it to startup time and lose
  filings accepted during downtime. The same cutoff applies to newly enabled
  companies. The approved initial cutoff is `2026-10-10T00:00:00Z`, inclusive.
- Backfill requires an explicit operator invocation and declared company/date
  scope. It must not start automatically during live startup or company reload.
- Backfill must preserve the same immutable accession/artifact identities and
  idempotent storage/publication rules. Rediscovering an acquired artifact must
  not create a second identity or an incidental republish.
- Existing pending work must pass the same eligibility checks during recovery.
  Skipped work leaves pending indexes without being marked acquired or published.
  Preserve stored bytes, published records and terminal/dead-letter evidence.
- Both operations must respect the existing aggregate SEC request budget. A
  separate operation does not authorize an independent concurrent request budget.
  Scheduling backfill while live ingestion runs remains an implementation choice
  requiring review.

## Implementation status

The cutoff/filtering change is implemented in source, with production rollout
pending. `LIVE_INGESTION_START_AT` defaults to the approved timestamp and is saved
in CDK configuration. The running production image still uses the earlier broad
policy until deployed. No AWS or GitHub settings were changed by implementation.

Phase 9 implements the configured cutoff and filtering of discovery/recovery.
It retains missed filings present in the SEC recent response; traversal of older
history after a longer outage is deferred. Backfill command design and validation
are tracked separately as DATA-003 in [BACKLOG](../BACKLOG.md).

Earnings-related filing and document selection is accepted in
[ADR 012](012-acquire-earnings-releases-and-financial-reports-selectively.md).
The focused implementation sequence is in [PHASE_9_PLAN.md](../PHASE_9_PLAN.md).
Neither operation may claim full-package acquisition after adopting a narrower
selection policy. Checkpoint reasons and structured logs make exclusions visible.

## Consequences

Live startup will no longer imply historical collection. Operators can choose
historical coverage deliberately, while the fixed cutoff preserves eligibility
across restarts. Separate backfill tooling is deferred; removing implicit history
collection does not require building it first.

Until the transition is implemented, the original acquisition behavior documented
in README and the Phase 2/4 contracts remains the deployed behavior.
