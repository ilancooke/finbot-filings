# Architecture decisions

These accepted decisions explain the current SEC acquisition service. IDs are
retained from the original ADRs. [ARCHITECTURE](ARCHITECTURE.md) defines the current
contracts; [DEPLOYMENT](DEPLOYMENT.md) defines operating procedures. A material
change should add a new numbered decision and state what it supersedes, rather
than silently rewriting the rationale. Original ADRs and migration plans remain
recoverable in Git.

<a id="adr-001"></a>
## ADR 001 — Calendar-driven per-company polling

**Accepted.** Use one calendar provider behind an interface to schedule aggressive
per-company SEC submissions polling around expected earnings. Outside windows,
use low-frequency safety polling. Calendar observations are hints; SEC is
authoritative for filings. Polling targets are subject to the shared request budget.

This keeps scheduling understandable for a curated universe and avoids a global
EDGAR feed before it is needed. Missing/wrong calendar dates can delay discovery;
request volume grows with coverage. Revisit when coverage grows into thousands,
active windows saturate the budget or a global feed becomes operationally simpler.

<a id="adr-002"></a>
## ADR 002 — One continuously running ECS/Fargate task

**Accepted.** Run one Fargate task with in-memory schedules, queues and a process
limiter. Retain local Docker execution for development. A long-running worker fits
the polling workload without Lambda/EventBridge orchestration or homelab operation.

One task limits availability/throughput and has continuous baseline cost. Revisit
for measured multi-task needs, disproportionate cost or a workload that becomes
naturally event/schedule driven. Adding a task also requires revisiting ADR 006.

<a id="adr-003"></a>
## ADR 003 — S3 bytes and DynamoDB metadata/state

**Accepted.** S3 owns immutable document bytes. Four DynamoDB tables own company
configuration, calendar expectations and filing/artifact checkpoints. Deterministic
keys use CIK/accession/original filename. Content hashes are outside current scope.

Key-based operational access needs no relational joins. Ad hoc analytics are less
convenient and new access patterns may need indexes or a separate analytical store.
Revisit for demonstrated relational/analytical requirements or integrity needs
that justify hashes. Encryption follows ADR 010.

<a id="adr-004"></a>
## ADR 004 — SNS fanout and consumer-owned SQS queues

**Accepted.** Publish generic metadata-only ArtifactReady events after durable S3
and DynamoDB storage. Each consumer owns its subscribed SQS queue, retries and
processing rate. A single shared queue would make independent consumers compete.

At-least-once delivery requires artifact-ID deduplication and stable versioned
events. Resource count grows with consumers. Revisit for routing/cross-account
requirements that justify an event bus. The ingestion failed-work queue is separate
from consumer queues and subscription DLQs; encryption follows ADR 010.

<a id="adr-005"></a>
## ADR 005 — Immutable point-in-time history

**Accepted; foundational.** Amendments/restatements create new accessions and
artifacts. They never replace original bytes or publication facts. This prevents
later information from leaking into historical research/backtests.

Downstream consumers choose latest-restated versus as-known-at-the-time values.
Append-only history costs storage but is a core correctness requirement, not a
normal optimization target. SEC acceptance time remains distinct from an observed
public-availability instant.

<a id="adr-006"></a>
## ADR 006 — One process-wide SEC request budget

**Accepted.** All SEC calls share one client/limiter and bounded in-memory work
queues. Polls, indexes, downloads, retries and redirects obey at most five request
starts per second across the task. Due times establish eligibility, not dispatch.

One serialized HTTP attempt and reused session keep enforcement simple; slow
downloads compete with discovery. This is not a global limiter for multiple tasks.
Revisit for observed starvation, required multi-task execution or strict
cross-process admission guarantees; do not add coordination speculatively.

<a id="adr-007"></a>
## ADR 007 — Application and CDK in one repository

**Accepted.** Keep tightly coupled application and `infra/cdk/` code together.
GitHub delivers application images; operators deploy infrastructure manually.
CDK baseline task configuration and application release revisions have different
owners and must be reconciled deliberately.

This reduces overhead for one maintainer. Revisit for shared infrastructure,
separate platform ownership or different permissions/release lifecycles.

<a id="adr-008"></a>
## ADR 008 — Conservative automatic recovery

**Accepted 2026-10-09; implemented and deployed.** Keep ECS automatic replacement
with active desired count one, minimumHealthyPercent zero, maximumPercent 100,
120-second stop timeout and a configured 150-second quiet period before SEC work
on every ECS process start. Local quiet delay defaults to zero. ECS health startup
allowance is 300 seconds; calendar readiness is separate from process liveness.
SIGTERM closes admissions for new SEC attempts, including retries/redirects and
limiter waiters; dispatched I/O/checkpoints can finish.

Controlled releases/maintenance wait for actual STOPPED tasks, settle the selected
revision at zero, then separately start it. Automatic recovery is conservative
protection, **not formal mutual exclusion**: control-plane task counts do not prove
a STOPPING process has finished. The quiet period alone adds 2.5 minutes; detection,
shutdown and provisioning add more. Durable recovery cannot restore timeliness.

Revisit through [ARCH-001](BACKLOG.md#arch-001--recovery-delay) when active-window
delay is unacceptable, overlap is observed or strict global enforcement/multiple
tasks become necessary. Compare verified handoff, fencing and centralized dispatch
using realistic failure tests; a lease alone does not prevent stale dispatch.

References: [ECS services](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/ecs_services.html),
[graceful shutdown](https://aws.amazon.com/blogs/containers/graceful-shutdowns-with-ecs/).

<a id="adr-009"></a>
## ADR 009 — Yahoo observations with replacement-only reconciliation

**Accepted 2026-10-09; implemented.** Daily 30-day Yahoo observations are scheduling
hints. Complete collection does not establish cancellation authority: omissions
preserve expectations. Only unique same-provider/CIK quarterly-title evidence can
link a replacement date. Persist and strongly confirm the replacement before
cancelling the unchanged old observation; preserve tombstones and ambiguity.
Authoritative fixture providers retain omission-based reconciliation.

Use pinned yfinance transport with raw query/schema/total evidence, bounded repeated
day slices and a separate executor/session/private temporary cache. Yahoo supplies
no stable event ID/update time; replacement hints are evidence, not fabricated IDs.
Satisfaction is date-specific. Strict consistency checks may reject unstable
refreshes; omissions can cause extra polling until normal window/grace expiry.
Application is not a multi-row transaction; later valid refresh repairs interruption.

Revisit the pinned private seam on dependency upgrades and reconciliation only when
new authoritative evidence supports a different rule. Successful collection does
not prove source accuracy, all-company coverage or ongoing access permission.
Yahoo access/reliability remains an operational follow-up in [BACKLOG](BACKLOG.md).

<a id="adr-010"></a>
## ADR 010 — Service-managed encryption without project KMS integration

**Accepted 2026-10-09; implemented and deployed.** Supersedes the original dedicated
SNS key, AWS-managed DynamoDB key and standard bootstrap asset-encryption choices.
Public regulatory documents and metadata-only events currently require no
customer-controlled keys or cross-account decryption governance.

| Service | Policy |
| --- | --- |
| Bootstrap and raw S3 | SSE-S3 (`AES256`) |
| DynamoDB | Default AWS-owned encryption (`TableEncryption.DEFAULT`) |
| Ingestion failed-work SQS | SSE-SQS |
| ArtifactReady SNS | Message bodies unencrypted at rest; HTTPS/scoped publish required |
| ECR and CloudWatch Logs | Service defaults; no custom keys |

No project KMS resources, aliases, clients, key identifiers or KMS Allow statements
are introduced. AWS services may use KMS internally. Preserve HTTPS, private
storage, immutable writes, retention/recovery protections and role separation.

Customized bootstrap derives from pinned CDK 2.1145.0, resource version 32, variant
`Finbot: SSE-S3 v1`. Preserve naming/qualifier, five roles, outputs and version
contract. The inert `FileAssetsBucketKmsKeyId=AWS_MANAGED_KEY` input exists for CLI
compatibility; it cannot select KMS. Preserve the protective lookup-role decryption
deny. Upgrades require comparison with upstream; deployed retained keys/old data
must never be deleted as incidental cleanup. See [bootstrap guidance](../infra/bootstrap/README.md).

Revisit for sensitive private data, regulatory/customer requirements, independently
controlled decryption, key-level audit or cross-account key governance.
References: [S3 encryption](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingServerSideEncryption.html),
[DynamoDB options](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/EncryptionAtRest.html),
[SNS encryption](https://docs.aws.amazon.com/sns/latest/dg/sns-server-side-encryption.html).

<a id="adr-011"></a>
## ADR 011 — Separate live ingestion from historical backfill

**Accepted 2026-10-10; live boundary deployed; backfill tooling deferred.** Normal
live discovery uses fixed inclusive acceptance cutoff `2026-10-10T00:00:00Z`,
saved in configuration. Restarts/deployments and newly enabled companies share it.
Live startup must not initiate incidental historical acquisition. Pending recovery
uses the same eligibility checks while preserving stored/published/terminal facts.

Explicit backfill must declare company/date scope, share immutable identities and
idempotent storage/publication, and respect the aggregate SEC request budget.
There is no independent concurrent request allowance. Current recent-submissions
coverage does not guarantee recovery after arbitrarily long outages.

This narrows broad acquisition from the POC/migration. Removing implicit history
does not require building backfill first. Plan the operation when historical
coverage is requested; see [DATA-003](BACKLOG.md#data-003--explicit-historical-backfill).

<a id="adr-012"></a>
## ADR 012 — Select earnings releases and financial reports

**Accepted 2026-10-10; deployed.** `earnings-acquisition-v1` selects 8-K/8-K/A with
unambiguous Item 2.02 evidence and periodic-report primaries including amendments.
Selected 8-Ks retain primary, identified releases and financial supplements in
HTML/text/PDF. Descriptions and primary exhibit references determine roles, not
fixed exhibit numbers. Missing/conflicting evidence remains visible.

Exclude decks, standalone images/logos, complete submission TXT bundles, unrelated
exhibits and auxiliary files. Do not follow external/asset links. Legitimate text
releases are allowed. Routing is structural, with no LLM, OCR or financial-value
extraction. Preserve policy/skip/unresolved reasons in existing checkpoints/logs;
no separate manifest/review store is needed. Selected documents retain the 64-MiB
acquisition limit; primary routing inspection is bounded to 4 MiB.

This intentionally narrows coverage: decks can contain unique guidance/KPIs and
image-only HTML is not fully preserved by retaining its markup. Completion means
selected documents, not full-package or complete earnings coverage. Recovery also
filters older pending acquisition/publication. Historical published objects/events
are not retracted, and terminal/dead-letter facts are neither deleted nor redriven.
Pre-filter images cannot safely read skip checkpoints and are unsuitable rollback
targets. Live selection of a newly eligible filing remained unobserved in the
last recorded startup inspection; see [OPS-002](BACKLOG.md#ops-002--observe-live-selection-and-daily-calendar-operation).

The current `earnings-acquisition-v2` revision refines attachment routing within
this same scope. Descriptive same-package links cover nonstandard exhibit-table
layouts. Generic news/press releases and quarterly supplements require exact
Item 2.02 exhibit references; explicitly labelled financial statements and
supplementary sales data also qualify. Evidence remains attachment-specific and
presentation/conflicting-role exclusions remain in force. Exact matching rules
are maintained in [ARCHITECTURE](ARCHITECTURE.md#filing-admission-and-document-selection).

Only affirmative v1 checkpoints may upgrade to v2 after re-evaluation; the first
affirmative reason and processing facts are preserved. Blocked work requires an
explicit reviewed operation, and completed packages do not automatically acquire
newly recognized attachments. This is an application release with no schema or
infrastructure change, not an acquisition reset or historical backfill.

The policy supersedes broad acquisition of supported forms/directory contents.
Revisit for explicit coverage needs, unsupported layouts or demonstrated ambiguous
routing. Examples motivating the rule:
[KDP release](https://www.sec.gov/Archives/edgar/data/1418135/000141813526000048/kdp-20260806.htm),
[CNA release/supplement/deck](https://www.sec.gov/Archives/edgar/data/21175/000002117526000043/cna-20260803.htm),
[EPR image-based deck](https://www.sec.gov/Archives/edgar/data/1045450/000104545026000022/q12026earningscallpresen.htm).

<a id="adr-013"></a>
## ADR 013 — Readable artifact folders and explicit acquisition reset

**Accepted 2026-10-11.** Supersedes ADR 003's CIK-only S3 location and ADR 011's
original cutoff value. Stable company/filing/artifact identities remain unchanged.
S3 locations become `<ticker>/<YYYY-MM-DD>_<form>_<accession>/<original-filename>`;
the date is SEC acceptance day in America/New_York, form slashes become hyphens,
and the canonical original artifact ticker is used. Filenames retain case.
Version 2 object provenance additionally records the parent acceptance timestamp.
Conditional creation, immutable bytes and metadata-only event schema 1.0 remain.

The user authorized restarting acquisition without downstream dependencies.
The inclusive fixed cutoff becomes `2026-10-09T04:00:00Z` (October 9 midnight New
York). This is an explicit reset scope, not automatic historical backfill. Normal
restarts and newly enabled companies keep the same value.

After validating/staging a compatible image at zero, delete all raw bucket object
versions/delete markers and all Filings/Artifacts records. Clear Calendar filing
satisfaction and historical failed-work messages, preserving Companies, calendar
expectations/tombstones/sync, resources, policies and logs. Reacquire through normal
recent-submissions discovery and the existing earnings selector. No rename/migration
or legacy-key fallback is introduced. Pre-reset images are unsuitable active
rollback targets. Preserve reset inventories and verification outside Git.

Ticker changes affect future discoveries but do not rename existing objects; CIK
remains identity. Revisit if historical symbol mapping or downstream consumers
require another presentation. The one-time reset is an explicitly authorized
exception to normal preservation rules, not a general repair procedure.

<a id="adr-014"></a>
## ADR 014 — Move the fixed live cutoff to October 6

**Accepted 2026-10-11; production configured 2026-10-12 UTC.**
Supersedes only ADR 013's cutoff value. The user requested the fixed inclusive
cutoff `2026-10-06T04:00:00Z` (October 6 midnight in America/New_York).
Application and CDK defaults and deployment inputs use this value; production
adoption follows the existing CDK/release procedure.

Normal recent-submissions discovery applies the existing earnings selector to
newly eligible filings. Existing bytes/checkpoints remain unchanged; previously
checkpointed exclusions do not automatically reopen. No data reset or historical
traversal is introduced. Restarts and newly enabled companies retain this fixed
value; explicit historical backfill remains deferred under ADR 011.

<a id="adr-015"></a>
## ADR 015 — Track production settings separately from private inputs

**Accepted 2026-10-11; implemented and deployed 2026-10-12 UTC.** Supersedes the single ignored
production configuration file. Track deployment settings, including the fixed
live cutoff and approved image digest, in `infra/cdk/config.prod.json`.
The ignored `config.local.json` retains only `account`, `sec_user_agent`, and
optional `alarm_email`, preserving the existing SEC and alarm contacts separately.

Select the public and private files explicitly through `FINBOT_INFRA_CONFIG`
and `FINBOT_INFRA_PRIVATE_CONFIG`. Reject private deployment overrides and
private fields in the public input when using the split. Keep standalone dummy
fixture synthesis supported with the matching public/private example pair and
without real private input or AWS access. Existing local
files require a one-time split; no fallback silently loads old configuration.

This makes production settings reviewable in Git while respecting the user's
public-repository privacy preferences. Existing historical account references
are outside this change. Normal CDK/release handoff and manual infrastructure
ownership remain unchanged; no AWS resources or durable data are changed by
editing these files.

<a id="adr-016"></a>
## ADR 016 — Match earnings on the expected New York date

**Accepted 2026-10-13.** Supersedes earnings-satisfaction-v1's use of the active
polling window as an acceptance constraint for new matches. Legitimate early
earnings filings can arrive before that window and still be downloaded, leaving
their calendar expectations unsatisfied.

New `earnings-satisfaction-v2` matches use the expected date in
`America/New_York`, from midnight inclusive to the next midnight exclusive,
independent of provider timing and polling/session configuration. Keep the same
CIK, original-form and unambiguous submissions Item 2.02 requirements, deterministic
candidate ordering and ambiguity safeguards. Do not reuse an already matched
accession for another expectation. Polling windows, safety cadence, acquisition
selection and document processing remain as defined previously.

Retain existing v1 checkpoints unchanged; both ingestion and dashboard readers
support v1/v2, while new v2 bounds are validated strictly. Normal recent-submissions
polls can link completed filings for loaded expectations without redownloading.
No data migration, reset, historical traversal or infrastructure change is needed.
This remains a scheduling heuristic rather than validated financial-result evidence.
