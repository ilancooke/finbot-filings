# finbot SEC ingestion

This package captures the current design for the SEC document discovery and download service that will act as the ingestion layer for the broader finbot platform.

The documents are intentionally scoped to **v0**. They preserve extension points for future scale and new downstream consumers, but avoid building distributed infrastructure before it is needed.

## Implementation status: Phase 1

`finbot_ingestion` provides offline domain contracts, configuration validation,
SEC URL construction, and submissions parsing. It coexists with the existing
`finbot_filings` package and `finbot-filings` CLI, whose behavior is unchanged.
See [README-legacy.md](README-legacy.md) for those local workflows.

The distribution is still named `finbot-filings`; both Python namespaces are
installed from this repository. No new runtime dependencies are required.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m compileall src tests
.venv/bin/python -m pytest
```

The new code is organized as follows:

- `domain/`: `Company`, `ExpectedEarningsEvent`, `Filing`, `Artifact`,
  `ArtifactReady`, identity validation, and UTC timestamp helpers.
- `sec/submissions.py`: pure parsing of supplied SEC JSON, adapted from the
  legacy submissions parser, without requests, ticker lookup, or a count limit.
- `sec/urls.py`: official submissions, filing-index, and document URLs.
- `config.py`: `IngestionConfig`, reading process environment or an injected
  mapping. It does not load `.env` automatically or require legacy output paths.

For example, parse a local submissions fixture without contacting SEC:

```python
import json
from datetime import datetime, timezone
from pathlib import Path

from finbot_ingestion.domain import Company
from finbot_ingestion.sec.submissions import parse_company_submissions

payload = json.loads(Path("tests/fixtures/submissions_mixed.json").read_text())
filings = parse_company_submissions(
    Company(ticker="AAPL", cik="320193", name="Apple Inc."),
    payload,
    discovered_at=datetime(2026, 10, 8, tzinfo=timezone.utc),
)
```

The parser accepts all six supported forms and preserves amendments under their
own accessions. It returns unique filings newest-first by `filed_at`, with
accession as a deterministic tie-breaker. Identical repeated accessions collapse;
conflicting metadata for one accession raises `SECDataError`.

`filed_at` currently means the supplied SEC `acceptanceDateTime`, normalized to
UTC; it is not a measured public availability time. Missing, invalid, or
timezone-naive acceptance timestamps fail explicitly. `filingDate` and
`reportDate` never supply a fallback timestamp. `primaryDocument` may be absent,
null, or empty; the resulting name is `None` for later package enumeration.
An invalid relevant row rejects the parse; callers must not treat that response
as fully processed. Supplied parallel columns must have matching lengths.

Artifact IDs are `<dashed-accession>/<original-filename>`, computed by `Artifact`
and `domain.identity.artifact_identity()`. Filenames preserve case, must be
basenames, and cannot contain path separators or control characters. No content
hash is used. `artifact_key()` produces `<10-digit-cik>/<artifact-id>`.

`ArtifactReady.from_artifact(filing, artifact)` requires matching filing identity
and populated `s3_uri`/`stored_at`; `to_dict()` and `to_json()` serialize the
version `1.0` contract with UTC timestamps and no document contents. This validates
the record, not an actual storage commit; the future worker must enforce the
S3 → DynamoDB → SNS ordering. Artifact storage checkpoints require `s3_uri` and
`stored_at` together, and publication requires a storage checkpoint.

Phase 1 configuration:

| Environment variable | Requirement |
| --- | --- |
| `SEC_USER_AGENT` | Required, nonempty identifying header; no fabricated fallback |
| `SEC_MAX_REQUESTS_PER_SECOND` | Defaults to `5`; must be finite, positive, and at most `5` |

Export these variables before calling `IngestionConfig.from_env()`, or pass a
mapping directly. `.env.example` retains legacy settings and documents the new
ceiling. This validates the future service's ceiling; it does not change the
legacy client's throttling or implement a rate limiter yet.

HTTP transport/rate limiting, filing-package enumeration, AWS repositories and
storage, SNS publication, recovery, calendar synchronization, scheduling,
observability, Docker/CDK, and CI/deployment remain later phases. There is no
`finbot_ingestion.main` runtime yet. The legacy source and existing data have not
been migrated or removed. Phase 1 unit tests block network connections and use
local fixtures only.

## Migration status

Phase 1 is complete. See [the migration plan](docs/MIGRATION_PLAN.md) for the
full eight-phase execution roadmap, acceptance criteria, validation record, and
next milestone: Phase 2 — shared SEC transport and complete package discovery.

## Documents

- [`docs/HLD.md`](docs/HLD.md) — high-level architecture, responsibilities, assumptions, data flow, AWS services, and scaling boundaries.
- [`docs/LLD.md`](docs/LLD.md) — implementation-oriented design for Codex: package structure, interfaces, data models, persistence, event schema, algorithms, retries, restart behavior, configuration, and tests.
- [`docs/adr/`](docs/adr/) — architecture decision records explaining the major design choices and when to revisit them.

## v0 summary

- Coverage: curated universe of approximately 500 companies.
- Earnings calendar: one free provider behind a swappable interface; provider selection is TBD.
- Discovery: calendar-driven, per-company SEC/EDGAR polling.
- Active polling: approximately every 5–10 seconds during earnings windows.
- SEC request ceiling: centralized client-side limit of 5 requests/second for the service.
- Relevant forms: 8-K, 10-Q, 10-K, plus amendments; download the primary filing document and all exhibits.
- Storage: immutable raw documents in S3; filing/artifact metadata and durable checkpoints in DynamoDB.
- Eventing: publish `ArtifactReady` to SNS after durable storage; downstream consumers receive through their own SQS queues.
- Runtime: one ECS/Fargate task for v0, with an in-memory scheduler/request queue.
- Observability: CloudWatch logs, metrics, and alarms; DLQ for exhausted retries.
- CI/CD: GitHub Actions runs tests, builds/pushes the Docker image to ECR, and updates ECS; CDK deploys infrastructure.
- Historical correctness: amendments/restatements create new immutable records so downstream clients can reproduce the information set available at any point in time.

## Explicitly deferred

- Global EDGAR latest-filings ingestion.
- SEC Company Facts ingestion.
- Multi-instance/distributed SEC rate limiting.
- Multiple earnings-calendar providers and reconciliation.
- RAG/indexing and earnings extraction logic; these are downstream consumers of this service.
