# High-Level Design — finbot SEC Document Ingestion Service

## 1. Purpose

`finbot-sec-ingestion` discovers, downloads, stores, and publishes metadata about SEC filing artifacts for a configured universe of public companies.

The service is intentionally responsible for **acquisition, not interpretation**. It does not extract earnings values, summarize management commentary, compute features, or build a knowledge base. Those are downstream concerns.

The service exists as a reusable platform component because the same SEC documents may feed multiple consumers, including:

- near-real-time earnings extraction;
- financial-statement parsing and feature computation;
- qualitative analysis of management discussion;
- future RAG/indexing workflows;
- future historical research and backtesting systems.

## 2. v0 goals

1. Support a curated universe of approximately 500 companies while preserving a path to thousands.
2. Maintain an earnings calendar and use it to determine when individual companies should be polled aggressively.
3. Detect relevant SEC filings quickly enough to support a downstream end-to-end earnings target of p50 < 30 seconds and p99 < 60 seconds.
4. Download the filing's primary document and exhibits once they become available.
5. Persist immutable raw artifacts and sufficient metadata so downstream systems never need to re-query SEC for already acquired content.
6. Publish a generic artifact event immediately after durable storage.
7. Remain restart-safe and idempotent.
8. Preserve publication history so downstream consumers can reconstruct what information was available at a historical point in time.

## 3. Non-goals for v0

- Cover the entire US equity universe automatically.
- Use the paid SEC Public Dissemination Service.
- Use the SEC Company Facts API as part of ingestion.
- Perform LLM extraction or any other document interpretation.
- Run multiple ingestion workers concurrently.
- Implement a distributed rate limiter.
- Ingest the global EDGAR latest-filings stream.
- Reconcile multiple earnings-calendar providers.
- Chunk documents for RAG.

## 4. Key assumptions

- The initial company universe is curated and contains a stable mapping of ticker to SEC CIK.
- One earnings-calendar provider will be used in v0. The exact provider is TBD and must be isolated behind an interface so it can be replaced.
- The earnings calendar is a scheduling hint, not a source of truth. EDGAR remains authoritative for actual filings.
- Companies may publish earlier/later than expected and calendar data may change.
- A filing may contain multiple child documents such as the primary filing, EX-99.1 earnings release, presentation PDFs, and other exhibits.
- Amendments and restatements are new filings with new accession numbers. Original filings are never overwritten.
- A single ECS/Fargate task is sufficient for v0 throughput.

## 5. System context

```text
Earnings Calendar Provider
          |
          v
   Calendar Sync
          |
          v
      DynamoDB
          |
          v
+-------------------------------+
| SEC Ingestion Service         |
| ECS/Fargate                   |
|                               |
| - calendar scheduler          |
| - SEC discovery client        |
| - global request limiter      |
| - artifact downloader         |
| - repositories                |
+---------------+---------------+
                |
        +-------+-------+
        |               |
        v               v
       S3           DynamoDB
 immutable raw      metadata /
 artifacts          checkpoints
        \               /
         \             /
          +-----+------+
                |
                v
           SNS Topic
        ArtifactReady
        /     |      \
       v      v       v
     SQS     SQS     SQS
  earnings   RAG    future
 extractor  indexer consumer
```

## 6. Major components

### 6.1 Company universe

A curated configuration of approximately 500 supported companies.

Minimum identity fields:

- ticker;
- CIK;
- company name;
- enabled/disabled status.

The universe is intentionally curated in v0. Automatic expansion and exchange-master reconciliation are deferred.

### 6.2 Earnings calendar adapter

Responsibilities:

- fetch upcoming expected earnings dates/times from one provider;
- normalize provider-specific fields;
- store/update expected events in DynamoDB;
- preserve provider raw values where useful for debugging;
- expose a provider-independent contract to the rest of the service.

The exact provider is TBD.

### 6.3 Scheduler

Responsibilities:

- load near-term earnings-calendar records from DynamoDB at startup;
- maintain short-term polling schedules in memory;
- place due companies into the internal request queue;
- reload durable state after restart.

The scheduler does **not** read DynamoDB every few seconds. Calendar data is refreshed on a coarse cadence and the active polling schedule is maintained in memory.

Initial polling policy:

- outside earnings windows: low-frequency safety polling only;
- before-market reporters: active polling begins roughly two hours before market open and continues roughly two hours after open;
- after-market reporters: active polling begins roughly two hours before market close and continues roughly three hours after close;
- unknown-time reporters: use a wider trading-day + after-hours window;
- during active window: target approximately 5–10 second polling cadence per active company, subject to the global SEC request ceiling;
- after a relevant filing is found: stop aggressive polling for that expected event;
- if no filing appears: continue through a grace period, then fall back to low-frequency polling.

These exact windows are configuration, not hard-coded business logic.

### 6.4 SEC client and centralized rate limiter

All SEC HTTP calls flow through one client and one shared request budget.

v0 policy:

- hard ceiling of 5 requests/second for the entire ingestion task;
- identify the application with an appropriate SEC User-Agent;
- reuse HTTP connections;
- exponential backoff for throttling, server errors, and network failures;
- scheduler eligibility does not guarantee immediate execution; the rate limiter controls actual outbound request timing.

The internal request queue is in memory because only one ECS task exists in v0.

### 6.5 Filing discovery

For companies inside active windows, poll the SEC per-company submissions endpoint and compare newly observed accession numbers against DynamoDB.

Relevant forms in v0:

- 8-K;
- 10-Q;
- 10-K;
- 8-K/A;
- 10-Q/A;
- 10-K/A.

Form type is only an initial relevance filter. The ingestion service stores the filing package; downstream consumers decide which documents are useful.

### 6.6 Artifact acquisition

A filing is the parent SEC submission identified by accession number.

A filing can contain multiple child artifacts, including:

- primary filing document;
- earnings-release exhibit (often EX-99.x);
- presentation/slides;
- other attached exhibits.

For each relevant filing, v0 downloads:

- the primary filing document;
- all attached exhibits/documents available in the filing package.

Documents are not chunked, transformed for RAG, or interpreted by this service.

### 6.7 S3 artifact store

Raw artifacts are immutable.

Suggested key pattern:

```text
s3://<bucket>/<cik>/<accession_number>/<filename>
```

The service does not overwrite historical documents. Amendments/restatements are stored under their own accession numbers.

Content hashing is explicitly deferred from v0. Identity is based on SEC accession number plus document filename.

### 6.8 DynamoDB metadata/state store

DynamoDB stores:

- earnings-calendar records;
- filing records;
- artifact metadata;
- durable processing checkpoints;
- publication timestamps;
- retry/failure metadata.

Only durable checkpoints are persisted. Short-lived states such as `downloading` remain in memory.

### 6.9 Event publication

After an artifact has been successfully stored in S3 and its metadata is durably written to DynamoDB, the service publishes a generic `ArtifactReady` event to SNS.

Each downstream consumer owns its own SQS queue subscribed to the topic.

This provides:

- fanout;
- independent retry policies;
- independent processing rates;
- no competition between consumers;
- easy addition of future consumers.

The ingestion service does not know whether a consumer is real-time or batch-oriented.

## 7. Data ownership and contracts

### 7.1 Filing identity

Primary identity: SEC accession number.

A filing record represents one immutable SEC submission.

### 7.2 Artifact identity

Primary identity: accession number + document filename.

An artifact is one document within a filing package.

### 7.3 Point-in-time correctness

The ingestion layer must preserve original publication history.

Example:

```text
Q3 filing published on 2026-10-31 -> stored permanently
Restatement published on 2027-02-15 -> stored as new filing
```

A downstream feature calculator asking for values "as of 2026-11-01" must be able to use only information that was available by that date. The ingestion service therefore never mutates history to make an old filing reflect a later restatement.

## 8. Restart safety and idempotency

Durable state is inferred from recorded facts rather than a high-frequency status machine.

For a discovered artifact:

```text
record exists, no s3_uri/stored_at
    -> acquisition incomplete; retry download/store

s3_uri/stored_at exists, no published_at
    -> artifact durable; retry SNS publication

published_at exists
    -> complete
```

The same filing/artifact may be encountered repeatedly without creating duplicate logical records.

## 9. Failure handling

### 9.1 Transient failures

Retry with exponential backoff for:

- SEC timeout/network failures;
- SEC 429/403/5xx responses where retry is appropriate;
- S3 write failures;
- DynamoDB write failures;
- SNS publish failures.

### 9.2 Terminal failures

Repeated failures that exhaust configured retry limits are surfaced through a dead-letter path and operational alerting.

### 9.3 Logging failures

Every failed attempt is logged, even when a later retry succeeds. Logs should include operation, timestamp, company/CIK, accession/artifact ID where available, attempt number, error, and eventual outcome.

## 10. Observability

Use CloudWatch for v0.

### Logs

Structured application logs for:

- polling;
- discoveries;
- downloads;
- storage writes;
- event publication;
- retry attempts;
- calendar sync;
- failures.

### Metrics

Track at minimum:

- discovery latency: SEC filing time -> discovered_at;
- download latency: discovered_at -> stored_at;
- ingestion latency: SEC filing time -> stored_at;
- request counts/status codes;
- throttle events;
- retry counts;
- terminal failure count;
- DLQ depth;
- artifacts stored;
- last successful calendar sync;
- ECS task/service health.

### Alarms

Alert on conditions such as:

- ingestion task not running;
- stale calendar sync;
- DLQ non-empty;
- repeated SEC failures;
- unusually high discovery latency.

## 11. AWS deployment

### Runtime

One ECS/Fargate service running one task continuously.

The container hosts:

- calendar refresh orchestration;
- in-memory scheduler;
- in-memory request queue;
- SEC client/rate limiter;
- downloader;
- persistence/event publication logic.

### AWS services

- ECS/Fargate — runtime;
- ECR — Docker image repository;
- S3 — immutable raw artifacts;
- DynamoDB — metadata and durable checkpoints;
- SNS — generic artifact fanout;
- SQS — owned by downstream consumers; DLQs where appropriate;
- CloudWatch — logs, metrics, alarms;
- IAM — least-privilege service access.

### Infrastructure as code

AWS CDK is preferred for v0 and lives in the same repository as the service.

## 12. CI/CD

Application delivery flow:

```text
merge to main
 -> GitHub Actions
 -> install dependencies
 -> run pytest
 -> build Docker image
 -> authenticate to AWS
 -> push image to ECR
 -> update ECS service
```

Initial infrastructure deployment remains manual via CDK. Infrastructure deployment may be automated later.

## 13. Scalability boundary

v0 is explicitly optimized for approximately 500 covered companies and one ingestion task.

Revisit architecture when one or more of the following becomes true:

- active earnings windows regularly exceed the 5 req/sec SEC budget;
- coverage grows into the thousands and per-company polling creates excessive request volume;
- one ECS task becomes a reliability/throughput bottleneck;
- queue depth causes discovery latency to miss targets;
- multi-instance availability becomes necessary.

Likely future changes:

- global EDGAR latest-filings ingestion;
- distributed rate limiting;
- multi-task ingestion workers;
- externalized scheduling/coordination;
- SEC Company Facts ingestion as a supplemental data product.

## 14. Security considerations

- No AWS credentials in source control.
- ECS task role receives only required S3/DynamoDB/SNS/CloudWatch permissions.
- GitHub Actions should authenticate to AWS using short-lived federation/OIDC rather than long-lived static credentials where practical.
- The service exposes no public inbound API in v0; its external interaction is outbound to SEC/calendar provider and internal AWS publication.

## 15. Open decisions

The following are intentionally unresolved:

1. earnings-calendar provider;
2. exact active-window timings and safety-poll cadence;
3. exact DynamoDB partition/sort key layout, finalized in LLD/implementation;
4. exact retry counts/backoff constants;
5. exact CloudWatch alarm thresholds.
