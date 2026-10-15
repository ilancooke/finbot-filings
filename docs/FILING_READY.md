# FilingReady 1.0

`finbot-filings` publishes one `filing.ready` notification after every selected
file in a filing is stored. Consumers process the listed files together.
This replaces per-document `ArtifactReady`. The producer topic is
`<prefix>-<environment>-filing-ready` (`finbot-prod-filing-ready` in production).
CDK exports it as `FilingTopicArn` and supplies `FILING_READY_TOPIC_ARN` to the
application. Earnings owns its subscribed SQS queue on this new topic.
Implementation is local; production activation is a separate release operation.

The notification contains references, not document bytes.

### Envelope

All fields below are required.

| Field | Type | Contract |
| --- | --- | --- |
| `event_type` | String | Exactly `filing.ready` |
| `schema_version` | String | Exactly `1.0` |
| `filing_id` | String | Dashed SEC accession |
| `snapshot_id` | String | Deterministic manifest SHA-256, specified below |
| `cik` | String | Ten-digit, zero-padded canonical company identity |
| `ticker` | String | Canonical upstream ticker |
| `company_name` | String | Reviewed upstream company name, frozen with the snapshot |
| `form_type` | String | Original SEC form, retaining amendment suffix when present |
| `filed_at` | UTC timestamp string | Canonical SEC acceptance time |
| `ready_at` | UTC timestamp string | First durable completion of this stored snapshot |
| `primary_artifact_id` | String | Primary report's identity, present exactly once in `artifacts` |
| `artifacts` | Array of objects | Nonempty complete selected manifest, sorted by `artifact_id` |

Use normalized UTC timestamps in `YYYY-MM-DDTHH:MM:SS.ffffffZ` form so equivalent
timestamps have one representation for snapshot hashing.

### Artifact entries

| Field | Type | Contract |
| --- | --- | --- |
| `artifact_id` | String | Required; existing `<accession>/<filename>` identity |
| `filename` | String | Required; safe original basename with case preserved |
| `s3_uri` | String | Required; canonical upstream stored-object reference |
| `size_bytes` | Integer | Required; nonnegative stored byte count |
| `document_type` | String | Optional; omit when unknown |
| `content_type` | String | Optional; omit when unknown |

There is no redundant artifact count. Unknown optional values are omitted, not
null. Every artifact must belong to the envelope's filing and company; artifact
identity must agree with accession and filename. Preserve canonical S3 URI
encoding from upstream.

### Readiness guarantees expected by the consumer

The notification lists a nonempty complete selection with unique artifact IDs
and exactly one referenced primary. Every listed document is already stored.
Completeness covers the selected snapshot, not every file in the SEC archive.
The consumer uses this list directly rather than inferring completion from an
S3 prefix, individual document events or a waiting period.

Snapshot contents and `ready_at` remain fixed across notification retries. Added
attachments create a new snapshot; previously published inputs remain immutable.
Delivery is at least once. The consumer must tolerate duplicate notifications.
Persistence and publication recovery are described below.

### Deterministic snapshot identity

Compute `snapshot_id` as the lowercase 64-character hexadecimal SHA-256 digest of
canonical UTF-8 JSON containing exactly these envelope fields:

```text
filing_id, cik, ticker, company_name, form_type, filed_at,
primary_artifact_id, artifacts
```

Include every artifact field present in the manifest. Sort artifact entries by
their case-sensitive `artifact_id`; sort all JSON object keys, use no insignificant
whitespace, emit Unicode as UTF-8 and preserve original string case. Normalize
timestamps before hashing. Exclude `ready_at`, `snapshot_id`, the SNS/SQS wrapper
and other transport metadata.

Equivalent input produces the same identity regardless of notification retries
or original array ordering. Added documents or changed hashed input fields produce
a different identity. This hashes the manifest, not document bytes, relying on
the producer's immutable-object contract.

## Producer persistence and recovery

Enumeration saves the sorted selected artifact IDs on the canonical Filings row
before readiness. The primary must be included. Readiness uses strong reads of
those exact IDs and their checkpoints, never an S3 listing or an index count.
Missing, unstored, blocked or terminal members prevent notification of a partial
filing. Acquisition errors retain the existing file-level retry and failed-work
handling; notification construction/publication uses the filing's PUBLISH budget.

The reviewed company name is frozen on initial discovery. For legacy pending work
without a name, the runtime reads the existing Companies record, including disabled
companies, and freezes its reviewed name before creating the notification.

The existing Filings table also stores immutable notification rows:

- `accession_number=__filing_ready__/<filing_id>/<snapshot_id>`
- `record_type=filing_ready`, `repository_schema_version=1`, numeric `revision`
- `event_json`: the exact canonical FilingReady payload
- `published_at`: first durable SNS acknowledgment, when present

These reserved keys are notification records, not SEC accessions. They have no
pending-index attributes. The canonical filing points to `ready_snapshot_id`;
`ready_published_at` records its acknowledged current snapshot. Completed selected
filings remain in the existing `PendingFilingEnumeration` index under `PUBLISH`
until acknowledgment. No new tables or indexes are required. The topic cutover
updates the task role publish permission and task environment through CDK.
Dashboard scans must skip the reserved notification keys.

The order is all S3 objects → stored checkpoints → immutable saved notification
→ SNS → notification/filing publication checkpoints. Retries recover the saved
payload and first ready timestamp. Lost acknowledgments may duplicate the same
event. Publication recovery never downloads stored files again.

Artifact `published_at` remains a compatibility field: for new work it means the
file was included in an acknowledged filing notification. The runtime repairs
these child checkpoints after the filing checkpoint, including after a restart.
`FilingsPublished` counts filing notifications; `ArtifactsPublished` counts newly
checkpointed included files. The existing `ArtifactPublishFailures` metric name is
retained for the current alarm and now counts filing publication failures.

## Later attachments and legacy state

A reviewed package addition extends durable membership and produces a new full
snapshot after all members are stored. Prior payloads and their publication
checkpoints remain in the reserved notification rows. The first enumeration's
timestamp/count and original stored bytes remain unchanged. Automatic completed
package rechecking and historical notification replay remain outside this change.

The targeted attachment recovery helper commits the complete reviewed membership
before restart, and supports interrupted execution with the same manifest. A saved,
unpublished snapshot must finish before membership changes. Resume old helper/image
operations with their compatible original versions; do not bypass a partial run
by changing its manifest or task pin.

Legacy pending files continue through the selector. Where the complete current
source-backed selection is known and all selected child records exist, the runtime
can establish membership without creating missing children. Missing newly recognized
attachments still need explicit recovery. Already completed historical filings are
not automatically republished simply because this release starts.

## Bounds and rollout

The complete UTF-8 notification is bounded to 256 KiB before saving/publishing;
DynamoDB records retain the existing 400-KB validation. A manifest that exceeds a
bound fails explicitly; it is never truncated or split into file notifications.

Update/restart the dashboard reader before releasing this ingestion version.
Use normal offline tests, rebuilt ARM64 container checks and application delivery.
The topic rename requires state/runtime CDK deployment as described in
[DEPLOYMENT](DEPLOYMENT.md#filingready-rollout). The old topic and export remain
during cutover; subscriptions do not move automatically. No data reset, cutoff
change, provider call or historical backfill is required. Older images that cannot
read filing PUBLISH work and the new checkpoints are incompatible rollback targets.
Downstream subscribers select
`event_type=filing.ready` from the JSON message body; the producer does not add
SNS message attributes. Standard SNS wrapping remains enabled unless the subscriber
chooses raw delivery; transport wrappers are outside the hashed payload.

A reproducible example of manifest hashing is in
`tests/unit/test_filing_ready_contract.py`.
