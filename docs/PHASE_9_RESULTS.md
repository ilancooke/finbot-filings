# Phase 9 results — Earnings acquisition filters

Status: Implemented in source; production rollout pending (2026-10-10).
Scope follows [PHASE_9_PLAN](PHASE_9_PLAN.md) and ADRs 011–012.

## Implemented behavior

- Fixed inclusive SEC acceptance-time cutoff: `2026-10-10T00:00:00Z`, saved in
  configuration and preserved across restarts. Newly enabled companies share it.
- Item 2.02 gates 8-K/8-K/A; periodic filings retain their primary report.
- Descriptions and safe primary exhibit references select releases/supplements.
  Decks, standalone graphics, complete submission TXT and auxiliary files are skipped.
- Discovery and pending ACQUIRE/PUBLISH recovery share the filters. Additive
  checkpoint decisions remove skipped work from pending indexes without changing
  original package counts, raw bytes, publication history or terminal evidence.
- Missing/conflicting evidence and unknown roles remain visible diagnostics.
  Structural primary parsing is bounded to 4 MiB; selected acquisition keeps the
  existing 64-MiB bound. Primary routing bytes reuse the normal immutable store.
  Routing and artifact processing share admission slots to bound retained bodies.
- No new AWS resources, backfill command, bulk migration or automatic redrive.

## Validation

Validation uses synthetic SEC-shaped fixtures, fake HTTP transports and mocked
SDK boundaries; application tests do not contact SEC/Yahoo/AWS. Representative
cases cover KDP-style releases/logos, CNA-style supplement/deck numbering, non-
earnings Alcoa-style items, text/PDF releases, inline results, ambiguity, nested
HTML layouts, date boundaries and amendments. Recovery tests cover legacy pending
ACQUIRE/PUBLISH, partial enumeration, restart/cache eviction, concurrent children,
lost skip acknowledgments and preservation of terminal/published history.
Concurrency tests also verify that routing waits while artifact slots are occupied.

| Check | Result |
| --- | --- |
| Application suite | 622 passed; 8 container tests skipped in this run |
| CDK infrastructure suite | 29 passed |
| Final ARM64 container integration suite | 8 passed; 9 non-container tests deselected |
| Strict CDK synthesis using example configuration | Passed with `--no-lookups` |
| Python wheel and source distribution build | Passed |
| Python compilation, actionlint, diff whitespace and documentation links | Passed |

The local image `finbot-ingestion:phase9` was rebuilt with the final source and
tested offline. It was not pushed to ECR. Production image digest and activation
remain separate release steps.

## Production delivery

Nothing was deployed or changed in AWS/GitHub by this implementation. Existing
production remains on broad acquisition. First rollout must stage with ingestion
stopped because historical images cannot validate skip checkpoints. Follow
[DEPLOYMENT](DEPLOYMENT.md#phase-9-filtering-rollout) one step at a time.
