# Phase 9 implementation plan — Filter earnings acquisition

Status: IMPLEMENTED IN SOURCE (2026-10-10); production rollout pending.
Production still uses broad recent-row and directory acquisition until release.

Policy: [ADR 011](adr/011-separate-live-ingestion-from-historical-backfill.md) and
[ADR 012](adr/012-acquire-earnings-releases-and-financial-reports-selectively.md).
All changes belong to `finbot-filings`.

## Scope

This is a filtering change to the existing ingestion pipeline. Implement two
small deterministic selectors and a configured live cutoff. Reuse the existing
repositories, downloader, checkpoints, retries and publication path.

| Filter | Rule |
| --- | --- |
| Date | Accept filings at or after a fixed UTC `LIVE_INGESTION_START_AT` |
| 8-K / 8-K/A | Require unambiguous Item 2.02 evidence |
| 10-Q / 10-K, including amendments | Keep the primary report |
| Selected earnings 8-K | Keep the primary plus identified earnings releases and financial supplements |
| Other documents | Skip decks, standalone images/logos, complete submission TXT, unrelated exhibits and auxiliary files |

The cutoff is a saved configuration value, not `now()` on startup or a rolling
lookback. Restarts reuse it, so filings accepted during downtime remain eligible
when present in the SEC response. It also applies to newly enabled companies;
there is no separate per-company boundary system in this change. The approved
initial value is `2026-10-10T00:00:00Z`, inclusive. Yahoo calendar dates continue to control cadence,
not eligibility.

Historical collection will no longer happen implicitly. A supported, explicitly
scoped backfill command is a separate deferred task, tracked as DATA-003 in
[BACKLOG](BACKLOG.md). This phase does not implement history-file traversal or
claim recovery of gaps beyond the SEC recent response's coverage.

## 1. Add the filters

- Filter submissions before creating pending filing work. Known 8-K item lists
  without 2.02 are excluded. Missing/conflicting evidence stays visible as an
  unresolved diagnostic rather than being guessed eligible or irrelevant.
- Preserve SEC document descriptions during package enumeration. Select release
  and supplement exhibits from their descriptions and, when needed, the primary
  report's exhibit references. Do not hardcode EX-99.1 or select every EX-99.x.
- If submissions item evidence is missing, use filing-index metadata and, when
  needed, bounded primary-report inspection. Reuse primary bytes if fetched for
  routing. Do not fetch excluded attachments to classify them.
- Ambiguous exhibit roles are skipped with an unresolved diagnostic; clearly
  selected primary/release documents can still proceed. A combined release/deck
  description needs a diagnostic rather than an arbitrary guess. Image-only
  content remains unsupported; no OCR, financial extraction or LLM classification.
- Record the policy version `earnings-acquisition-v1` and concise reasons in
  structured logs and the existing checkpoint records where needed. No separate
  selection-manifest store or review-management system.

## 2. Apply the same rules to recovery

Discovery alone is insufficient: existing pending artifacts can be resumed
independently after a restart.

Use the same selectors before acquiring or publishing pending work. Older rows
without sufficient selection metadata are checked using their parent filing and
SEC package metadata. Save the small eligibility/skip checkpoint needed to avoid
checking or recovering an excluded item indefinitely; update existing pending-index
logic and validators accordingly. Use existing conditional/revision-checked writes.

Keep stored bytes, published records and original terminal/dead-letter evidence.
A skip is neither a download failure nor successful publication. Do not rewrite
old package snapshot counts or timestamps. Previously published artifacts remain
complete, and the four terminal submission-TXT failures are not redriven or deleted.
No bulk migration command or queue cleanup is part of this change.

## 3. Test and deploy through the existing process

Use offline fixtures and fake transports to verify:

- KDP keeps the primary and earnings release, without requesting logos or the bundle.
- CNA keeps the financial supplement even when it is EX-99.2, and skips the deck.
- Alcoa's transaction 8-K without Item 2.02 is excluded.
- Date boundaries, amendments, periodic primaries, text/PDF releases and ambiguous
  metadata follow the agreed rules.
- Recovery cannot acquire/publish excluded pending work; skips leave the pending
  indexes, survive restart and preserve stored/published/terminal facts.
- An excluded oversized document is never fetched; a selected oversized document
  still follows the unchanged 64-MiB failure behavior.

Add the cutoff to the existing CDK configuration/task environment and update
README/HLD/LLD to match implemented behavior. No new tables, indexes, IAM roles,
services, global locks or release workflow are planned.

Run the package checks, build/test the ARM64 image and use the existing staged
release and stop/wait/start deployment process. Review representative selection
logs and selected artifact checkpoints after activation. Choose a rollback image
that understands any added skip checkpoints; the old broad-acquisition image
would restore the behavior this change is intended to remove.

## Completion

Live discovery and recovery obey both filters, skipped files cause no download
alarms, and selected documents still use the existing durable acquisition path.
Historical backfill tooling, outage-history traversal, elaborate selection
journals and migration machinery are outside this phase.

Implementation is in `selection/policy.py`, package metadata parsing, discovery,
worker/recovery checks, additive checkpoint fields and existing CDK configuration.
Structural primary inspection is bounded to 4 MiB, without reducing the 64-MiB
selected-document acquisition limit. Representative selector/recovery fixtures
are offline. See [DEPLOYMENT](DEPLOYMENT.md#phase-9-filtering-rollout) for the first
release procedure and [PHASE_9_RESULTS](PHASE_9_RESULTS.md) for validation.
