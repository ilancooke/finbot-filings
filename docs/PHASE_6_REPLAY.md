# Phase 6 offline capacity replay

Historical measurements from 2026-10-08 with repository-local Python 3.14; eight
scenarios passed with the original driver. The driver correction below changes
test synchronization; the saved historical figures have not been regenerated.
[Machine-readable results](PHASE_6_REPLAY_RESULTS.json) retain per-active-company
attempt counts/interval percentiles, HTTP class/status counts, queue maxima, task
count, latency sample counts, database calls and recovery outcome.

## Method

The test runs the actual RuntimeApplication, WorkQueue, discovery/enumeration/
artifact worker, recovery, SecClient, shared limiter and bounded SEC executor.
Low-level SDK boundaries are stateful offline fakes; no AWS/SEC/provider calls
occur. It seeds 500 enabled synthetic companies plus one disabled company with
old stored/unpublished work. Five, ten, 25 or 50 companies have active expectations,
with either five- or ten-second completion-based target polling. The supplied
8-K has Item 9.01, so it is ingested while its expectation remains unsatisfied.

Each scenario advances 120 virtual seconds at 0.1-second resolution. Transport
metadata responses take 20 virtual milliseconds, downloads 400 milliseconds;
one poll gets HTTP 429 and retries through the same limiter. Normal packages have
four documents. The 50-active/five-second case adds 32 exhibits (36 documents).
Repeated recovery runs every 30 virtual seconds. The old disabled company's
publication index entry stays hidden until virtual second 60 and is then recovered.

On 2026-10-09, GitHub CI exposed a timing dependency in the original driver:
each simulated tick gave worker threads only one millisecond of wall time before
advancing again. Slower mocked SDK execution could exhaust the simulated window
before all companies were polled or delayed publication recovery completed.
The driver now waits for runtime tasks to reach explicit wait boundaries before
advancing simulated time. Mock SDK calls must finish; pending SEC calls can remain
on the single shared executor when its thread is waiting for a future replay-clock
deadline. Queue waits and async clock sleeps are tracked through test-only wrappers.
The existing eight capacity scenarios retain their assertions. A ninth scenario
(25 active companies, ten-second target) adds three milliseconds of wall delay to
each SDK execution to exercise the previously runner-sensitive boundary. That
delay is test synchronization stress, not modeled AWS latency or virtual time.

The default eleven required loops are retained, with two poll workers, one
enumeration worker, two artifact workers and one serialized blocking SEC executor.
Safety polling is staggered across the remaining enabled companies; those become
due during the simulation according to CIK. All request classes share the same
five-request/second ceiling, including retries and recovery.

## Observations

All durations below are seconds. Queue delay measures admission to worker start;
interval measures actual SEC request starts per active company, including retries.
Aggregated percentiles pool the samples; per-company values are in the JSON file.
These are observed results, rounded to one decimal place.

| Active | Target interval | Poll queue p99 | Actual interval p50 / p99 | Max poll queue | HTTP attempts | DynamoDB calls |
| --- | --- | --- | --- | --- | --- | --- |
| 5 | 5 | 0.1 | 5.2 / 5.5 | 3 | 153 | 2241 |
| 10 | 5 | 1.0 | 5.2 / 5.8 | 8 | 263 | 2251 |
| 25 | 5 | 3.8 | 5.5 / 9.1 | 23 | 470 | 2261 |
| 50 | 5 | 14.3 | 15.0 / 20.1 | 48 | 450 | 2783 |
| 5 | 10 | 0.3 | 10.2 / 10.5 | 3 | 96 | 2197 |
| 10 | 10 | 1.2 | 10.2 / 10.9 | 8 | 151 | 2207 |
| 25 | 10 | 4.4 | 10.2 / 11.4 | 23 | 316 | 2233 |
| 50 | 10 | 10.4 | 13.0 / 15.0 | 48 | 470 | 2275 |

Stage latency observations follow. There is only one newly discovered
filing per scenario, so discovery p50/p99 are the same single observation. Storage
and ingestion samples number four per normal package and 36 for the heavy case.
`DownloadLatencyMs` measures artifact discovery to storage and `IngestionLatencyMs`
measures SEC acceptance to storage; publication completion is asserted separately.
These small sample sizes do not establish a
production tail-latency distribution.

| Active | Target | Acceptance to discovery (one sample) | Artifact discovery to storage p50 / p99 | Acceptance to storage p50 / p99 | Artifact samples |
| --- | --- | --- | --- | --- | --- |
| 5 | 5 | 1.2 | 1.2 / 1.6 | 3.2 / 3.6 | 4 |
| 10 | 5 | 2.3 | 2.0 / 2.5 | 4.0 / 4.5 | 4 |
| 25 | 5 | 6.2 | 7.3 / 8.2 | 9.3 / 10.2 | 4 |
| 50 | 5 | 12.1 | 27.4 / 41.0 | 29.4 / 43.0 | 36 |
| 5 | 10 | 1.2 | 0.9 / 1.3 | 2.9 / 3.3 | 4 |
| 10 | 10 | 2.3 | 2.0 / 2.5 | 4.0 / 4.5 | 4 |
| 25 | 10 | 6.2 | 6.3 / 6.8 | 8.3 / 8.8 | 4 |
| 50 | 10 | 12.1 | 13.0 / 13.9 | 15.0 / 15.9 | 4 |

Every case asserts the global rolling SEC ceiling, bounded poll/filing/artifact
queues, eleven runtime tasks, service to every active company, successful artifact
storage and publication of the old disabled-company work after delayed visibility.
Final recovery summaries show zero remaining indexed candidates after that repair;
this is not a claim that an empty eventually consistent query proves completion.
Each case records one HTTP 429; successful remaining attempts use HTTP 200.

At 25 active companies with a five-second target, submissions alone would consume
the whole five-request/second budget. Indexes, downloads, retries and safety polls
compete for that budget. The heavy 50-active case stretches polling toward roughly
15-second median intervals. Queue growth stays bounded and all classes progress,
so the implementation retains FIFO dispatch and existing bounded workers. This
replay did not justify an added fairness layer or canonical-filing cache.

## Limits and reproduction

The data are synthetic, not historical SEC captures or production measurements.
Scheduling/thread interleavings and 0.1-second driver resolution introduce small
run-to-run variation. Mock SDK calls do not model AWS network delay/throttling or
billing. Database counts include universe seeding, startup hydration, calendar
metadata, discovery/checkpoints and recovery, and are not a cost estimate. Only one
new filing per scenario is modeled; repeated historical submissions in a real
universe may materially increase conditional writes and reads. Real transport
latency, document size and calendar/provider availability can further reduce
throughput. The five-request/second ceiling is a maximum, not promised throughput.

Satisfaction/restart, date moves, partial writes, loop failure, signals and repeated
cancellation are exercised separately by the Phase 6 integration tests and existing
restart tests, rather than inferred from this capacity replay. A live calendar
provider and authoritative production universe remain unresolved inputs.

```bash
.venv/bin/python -m pytest -q -s tests/integration/test_phase6_replay.py
.venv/bin/python -m pytest -q tests/unit/test_ingestion_phase6.py tests/integration/test_phase6.py
```
