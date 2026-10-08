# Phase 4 implementation plan

Status: implemented. This original plan is retained for reference; actual shipped
contracts, validation and limitations are in LLD.md and MIGRATION_PLAN.md.

This plan expands Phase 4 of MIGRATION_PLAN.md. HLD, LLD and accepted ADRs
remain the architectural sources of truth. Work is confined to finbot-filings.

## Outcome and scope

Deliver an independently testable path:

```text
submissions → durable filing → validated package → durable children
→ SEC bytes → immutable S3 object → DynamoDB storage checkpoint
→ SNS ArtifactReady → DynamoDB publication checkpoint
```

Expose async service methods and explicit recovery passes. Production queues,
continuous recovery cadence, calendar providers, scheduling, main runtime,
container cutover, CDK and deployment remain later phases. Preserve legacy code
and shared data. Routine validation contacts neither SEC nor AWS.

## 1. Finalize checkpoint and adapter contracts

Document concrete Phase 4 interfaces in LLD before dependent services are built.

- `ArtifactStore.inspect(artifact)` returns `StoredArtifact | None`.
- `ArtifactStore.put_if_absent(artifact, downloaded)` returns `StoredArtifact`.
- `StoredArtifact` carries URI, original S3 commit time, content type and byte
  count. Inspection and creation use the same validation and timestamp rules.
- `ArtifactEventPublisher.publish_artifact_ready(event)` publishes the existing
  event schema 1.0. Require a valid SNS response before checkpointing success.
- `DeadLetterPublisher.send(failure)` sends a versioned metadata-only failure
  envelope with a stable failure ID, work identity, stage, bounded error details,
  terminal time and references needed for operator investigation.
- Add an artifact processing checkpoint separate from immutable source metadata;
  extend FilingCheckpoint for stage retry and terminal/dead-letter facts.

Keep SDK objects behind adapters. Continue to construct clients explicitly, reuse
low-level Boto3 clients and offload blocking calls through bounded executors.
Reuse the existing DynamoDB execution behavior; extract a small shared helper
only if it removes actual duplication without changing existing semantics.
Offload synchronous SEC calls as well. Cancellation must retain admission until
an in-flight blocking call finishes; service cancellation never becomes a terminal
work failure.

## 2. Extend durable failure and recovery semantics

Keep four tables and existing GSI key definitions. Add optional checkpoint fields
that old Phase 3 records can omit:

- Failed workflow-attempt counts and idempotent failure observations per stage:
  enumeration, acquisition and publication. Existing retry_count remains an
  operational diagnostic; it is not the budget for every later stage.
- Paired terminal stage/time plus a stable bounded failure envelope.
- Optional dead-letter-sent time.

Use existing revision-guarded updates. Atomically commit failure count and any
terminal decision against the expected unfinished stage. Repeating one logical
failure update must not count twice. A late failure cannot terminalize a stage
that has advanced or completed, and terminal records cannot silently resume.

Extend existing sparse indexes to query an additional `DEAD_LETTER` work kind:

```text
unfinished normal work                  → ENUMERATE / ACQUIRE / PUBLISH
terminal, dead-letter send incomplete   → DEAD_LETTER
terminal, dead-letter send checkpointed → no pending-work keys
```

Commit terminal facts before sending to SQS; checkpoint the send afterward. Failed
sends remain indexed. Lost send acknowledgments may produce duplicate envelopes
with the same failure ID. Retain original durable source/storage facts throughout.
Future CDK will provision the existing indexes and operational queue; this phase
only updates adapter/schema documentation.

A filing can become terminal before any artifact exists, satisfying pre-artifact
failure recovery. Children of unfinished or terminal enumeration must not publish;
workers recheck the parent checkpoint and defer those candidates while continuing
the pass. Partial children retain their original facts. A terminal parent supplies
the operational failure record for its blocked children.

Submissions fetch/parse failures have no successful discovery checkpoint and are
surfaced to the caller for a later company poll. They must not mark a response
processed or invent a filing identity. Credential, permission, missing-resource
and other shared configuration errors fail the invocation visibly rather than
dead-lettering the whole universe. Automatic operator redrive is outside Phase 4;
document terminal investigation and the need for an explicit future redrive action.

## 3. Implement immutable S3 storage

Add `storage/{artifact_store,s3_artifact_store,errors}.py` with injected clients.

- Use the existing key helper: `<10-digit-cik>/<dashed-accession>/<filename>`.
- Inspect before downloading whenever the database has no storage checkpoint.
- Create with direct `PutObject(IfNoneMatch="*")`, preserving response-content
  bytes. Do not expose overwrite/delete or use an upload convenience path that
  cannot guarantee conditional creation.
- Persist a versioned, bounded, header-safe provenance envelope with the original
  identity, source URL and discovery facts. Validate required metadata before
  trusting an existing object. Do not rewrite incompatible/unverifiable objects.
- Use `HeadObject` LastModified as stored_at and ContentLength as size_bytes for
  both new objects and recovery. Always derive the checkpoint from inspected S3
  facts; never use restart time as the original storage time.
- Treat 412 as an inspect-and-reconcile path, 409 as a bounded conditional retry,
  and ambiguous transport outcomes as requiring inspection before another write.
- Only an unambiguous missing-object result permits download. A 403 is not proof
  of absence. Document required GetObject, conditional PutObject and scoped
  ListBucket permissions for reliable absence detection, with no delete rights.

Validate S3 key/metadata/body limits explicitly. Keep v0 on a bounded single-PUT
path; unsupported oversized artifacts fail visibly rather than use an unsafe
multipart fallback. Choose and document an application memory bound during
implementation, enforcing downloads early enough to respect it. No application
content hashes, ETag-based identity, transformations or content interpretation.

Storage uses HTTPS and bucket encryption; bucket policies, retention, access
logging, CloudTrail data events and operational metrics remain Phase 8 work.
Inspection adds S3 requests and checkpoints add DynamoDB writes; no cost estimate
is inferred from the SEC request ceiling.

## 4. Implement SNS and operational SQS adapters

Add `messaging/{publisher,sns_publisher,dead_letter}.py`.

Send ArtifactReady.to_json() as the plain SNS Message string, not an SNS
protocol-specific MessageStructure envelope. Use a standard topic and preserve
event schema 1.0. Validate region/resource configuration and bounded UTF-8 payloads.
Log failures and recovery without document bodies or credentials.

Send terminal envelopes directly to a configured operational SQS queue. This is
the application's failed-work queue, separate from consumer queues and SNS
subscription delivery DLQs. Do not create subscriptions or consume/redrive the
queue in this phase. Both event publication and dead-letter sends are at least
once; consumers deduplicate ArtifactReady by artifact_id.

## 5. Compose discovery and artifact processing

Add `ingestion/{discovery_service,artifact_downloader,ingestion_worker}.py`.

Discovery uses create_if_absent and then reloads the canonical filing/checkpoint.
Existing accession numbers must not suppress unfinished enumeration. Fetch a
fresh validated package and persist every child through PackageCheckpoint before
dispatching acquisition. Dispatch known snapshot identities using strong reads;
do not rely on immediate accession-index visibility. Preserve canonical parent
ticker and first discovery observations.

Artifact processing always reloads durable records and parent eligibility:

1. Published or terminal work takes no normal acquisition/publication action.
2. Without a database storage checkpoint, inspect S3 first. Reuse a compatible
   object, or download and conditionally create one when absent.
3. Commit mark_stored from inspected object facts, then strongly reload it.
4. Build ArtifactReady from the canonical parent and durable stored child.
5. Publish through SNS, then commit mark_published.

After any ambiguous checkpoint acknowledgment, strongly reload durable facts
before repeating external operations. Publication failure never triggers another
download. If SNS succeeded but its checkpoint was lost, republishing the identical
logical event is allowed. Per-identity in-process suppression prevents overlapping
calls; no distributed lease or production queue is introduced.

Use centralized jittered retry timing with explicit, finite SDK and workflow
budgets. SEC transport already owns per-request retries; avoid multiplying them
with an unbounded outer loop. Persist failed workflow attempts across restart and
apply separate stage budgets. Failed failure-checkpoint writes remain visible
errors and cannot be reported as successful dead-letter handling.

## 6. Add explicit recovery passes

Add `ingestion/recovery_service.py` with an injected worker and a `run_pass()`
method, without a continuous loop. Recover pending enumeration, acquisition,
publication and dead-letter sends. Continue every page to token None, including
empty actionable pages. Recheck source facts before acting; failed or deferred
items cannot stop unrelated work or restart pagination at the oldest candidate.

Keep recovery independent of company enabled status and discovery age. Return a
bounded pass summary suitable for tests and later runtime logging. Test a later
full pass discovering delayed GSI entries. One empty pass is not proof of global
completion. Reconciliation of previously completed SEC package snapshots remains
a later policy; later explicitly supplied snapshots can still add missing children.

## 7. Verify every durable boundary and update documentation

Use SDK Stubber for wire contracts and stateful fakes for commit/acknowledgment
loss. Extend existing offline integration/replay patterns and legacy byte/repair
regressions. Cover:

- Partial child creation and failed enumeration commit, then restart.
- Duplicate discovery, canonical ticker aliases and separate amendment accessions.
- Exact binary bytes, conditional-create races, 409/412, absent/forbidden objects,
  unsafe metadata, mismatched provenance and size limits.
- S3 success with a lost acknowledgment or failed database checkpoint: no byte
  replacement, extra SEC download or new storage provenance.
- SNS failure, lost SNS acknowledgment and lost publication checkpoint: same
  event identity and no reacquisition of stored artifacts.
- Terminal enumeration before children, blocked partial children, terminal
  acquisition/publication, failed DLQ send and lost DLQ checkpoint.
- Retry budgets surviving restart, late failures after progress, disabled
  companies, old pending work, empty pages, stale/delayed indexes, cancellation
  and responsive event-loop behavior.

Add isolated storage/messaging/workflow configuration without making existing
SEC/legacy commands require AWS settings. Update README, .env.example, LLD and
MIGRATION_PLAN with actual shipped behavior, schema additions and limitations.
ADRs need updates only if an accepted architectural decision changes. Ensure the
declared Boto3 minimum supports conditional PUT; the installed 1.43.110 SDK does.

Acceptance requires the complete offline pipeline and failure/restart replay,
all existing regressions and these checks:

```bash
.venv/bin/python -m compileall src tests
.venv/bin/python -m pytest
git diff --check
```

Record fresh results and test counts only after execution. Leave Phase 4 marked
not started during planning and mark it complete only after its migration-plan
acceptance criteria are met. Phase 5 remains separately scoped.

## AWS behavior checked while planning

- [S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)
- [PutObject](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html)
- [HeadObject and permission-dependent missing-object responses](https://docs.aws.amazon.com/AmazonS3/latest/API/API_HeadObject.html)
- [SNS Publish](https://docs.aws.amazon.com/sns/latest/api/API_Publish.html)
- [SQS SendMessage](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_SendMessage.html)

These were read through AWS MCP. No AWS resource inspection or mutation was
performed. No tests were run during planning.
