# Yahoo earnings-calendar implementation plan

Date: 2026-10-09. Status: IMPLEMENTED AND VALIDATED OFFLINE.
The original implementation sequence below is retained; section 5 records delivery.

## 1. Scope and intended behavior

Implement the Yahoo/yfinance handoff in [MIGRATION_PLAN section 5.1](MIGRATION_PLAN.md#51-yahooyfinance-earnings-calendar-implementation-handoff-2026-10-09)
inside `finbot-filings`. Use Yahoo observations as scheduling hints for the curated
CIK universe, preserving the existing SEC acquisition and satisfaction contracts.

The resulting runtime will support `CALENDAR_PROVIDER=yahoo` as well as the
explicitly unavailable placeholder. The Yahoo configuration will use 30 inclusive
calendar days, a completion-based 86,400-second full-refresh cadence, and disabled
near-term refresh. Settings remain configurable. A refresh starting on market-local
date D covers D through D+29; Yahoo queries use D+30 as the upper boundary and
explicitly filter normalized dates.

Successful collection does not give Yahoo authority to cancel absent records.
Only a validated, unambiguous replacement can deactivate an old expectation, after
the replacement is confirmed durable. Otherwise the old event remains available
until its polling window/grace ends; safety polling continues. Multiple fiscal
announcements for one CIK remain independent.

This work includes implementation, offline application/infrastructure validation,
and rebuilding/testing the Python 3.12 Linux ARM64 container. Production universe
selection, company seeding, live provider calls, AWS deployment, GitHub delivery,
and activation remain separate work. No other Finbot repository is affected.

## 2. Planning baseline: findings from code and dependencies before implementation

- `calendar/contracts.py` gives `CalendarSnapshot` a completeness flag but no
  separate reconciliation authority. `CalendarSyncService.sync_once()` currently
  cancels every missing in-scope event after any complete snapshot.
- `repositories/dynamodb/calendar.py` guards observations with `synced_at` and
  stores inactive tombstones. Upserts return no applied-state result; cancellation
  rechecks identity/time but does not require the exact event originally matched.
  Replacement handling needs stronger confirmation at those boundaries.
- `main.py` rejects every provider except `placeholder` and owns client/executor
  lifetimes through `ExitStack`. `BlockingExecution` already retains executor
  admission until blocking work finishes, including repeated cancellation.
- `runtime/application.py` already requests a scheduler reload after successful
  refresh and reconstructs satisfaction from durable records. Refresh ranges and
  full-coverage health currently use the UTC date; scheduler windows use the market
  date. Restart also requires a full rolling lookahead through the current date,
  which can trigger extra refreshes after midnight even when yesterday's daily
  collection is still within cadence.
- `.env.example` has 90-day lookahead and placeholder settings. CDK's
  `ingestion_stack.py` hardcodes the placeholder. Neither currently selects Yahoo.
- Phase 5 tests intentionally require omission cancellation for complete fixture
  snapshots. Keep that behavior for the existing authoritative-snapshot policy;
  add Yahoo tests rather than weakening those assertions.
- The existing Phase 7 image checks assert that lxml is absent. Adding yfinance
  changes that dependency contract and requires deliberate updates.

Planning inspected the installed probe release `yfinance==1.7.0`, including its
`calendars.py`, and the retained probe report. The public calendar method exposes
pagination and fresh-read options but returns a DataFrame; raw response metadata
is needed for collection validation. The installed method does not expose its own
HTTP timeout parameter. The transport boundary must enforce timeouts, attempt
budgets and pacing, including cookie/crumb initialization and internal retries.
See the [official calendar API](https://ranaroussi.github.io/yfinance/reference/api/yfinance.Calendars.html)
and [upstream source](https://github.com/ranaroussi/yfinance/blob/main/yfinance/calendars.py).
Online `main` is reference material, not the dependency version contract.

The temporary probe directory is useful evidence but is not a runtime or test
dependency. Synthetic fixtures will encode the relevant response shapes/anomalies;
cookies, caches, credentials and raw live datasets will not enter the repository.

## 3. Implement in this order

### Step 1 — Record contracts and compatibility before code changes

Add a new ADR for Yahoo scheduling hints and replacement-only reconciliation;
update HLD calendar assumptions and LLD sections 3, 4 and 7. Distinguish planned
behavior from delivered behavior until validation is complete.

Separate these concepts explicitly:

1. **Collection completeness:** all requested source slices/pages were validated.
2. **Reconciliation policy:** whether missing events may be cancelled, or only
   matched replacements may be cancelled.
3. **Replacement evidence:** a source-derived semantic hint linking two
   observations, without claiming Yahoo supplied a stable event ID.

Introduce a typed reconciliation policy/plan boundary. The existing fixture and
future authoritative providers retain the current absence-authoritative behavior;
Yahoo is wired explicitly to replacement-only behavior. Policy selection belongs
to the provider factory, with validation against the provider name. Do not let a
payload silently opt itself into stronger cancellation authority.

Keep `CalendarSnapshot.complete` and exact date/company scope validation. An
incomplete fetch cannot write expectations or advance successful freshness;
attempt/failure checkpoints may still be recorded. Validate the full normalized
batch and replacement plan before the first expectation write.

Add optional normalized replacement evidence to event persistence if the title
evaluation below supports it. It must be typed, versioned and distinguishable from
`provider_event_id`; old rows read as having no evidence. Keep Yahoo
`provider_event_id` and `provider_updated_at` null. Include serialization, equality,
size validation and reader compatibility in the contract update.

No new table/index, ticker-only key, calendar TTL, or physical deletion is needed.
Keep the existing `(expected_date, cik)` key. Two materially different events for
the same CIK/date cannot be represented independently by that key; reject such a
snapshot explicitly rather than silently discard an event or expand the schema.

### Step 2 — Build a bounded Yahoo client and prove collection completeness

Create provider-specific client/config/parser modules under `calendar/providers/`
and a `YahooEarningsCalendarProvider` behind the existing async interface. Pin
`yfinance==1.7.0` initially, subject to Python 3.12 Linux ARM64 compatibility checks.
Document lxml/curl_cffi as dependencies of the calendar integration; they do not
authorize downstream extraction functionality.

Use `yf.Calendars.get_earnings_calendar(filter_most_active=False,
market_cap=None, force=True)` with explicit offsets. Supply an owned, instrumented
curl_cffi session which enforces bounds and captures only the visualization
response's data/schema/criteria/total for the corresponding page. Convert that
evidence to private typed page records. Do not monkeypatch yfinance globally or
depend on its DataFrame alone for completeness. If transport interception cannot
reliably associate responses with pages, isolate a pinned raw-client seam within
the Yahoo adapter and document/test the exact dependency on it before proceeding.

Initial collection algorithm:

1. Divide the requested interval into market-local calendar-day slices. Query each
   day's start through the following midnight. Validate the raw echoed query bounds,
   region/event criteria, requested offset and required column identifiers.
2. Enumerate offsets with a configured page size at most 100. Require consistent
   nonnegative integer totals and schema across pages. Validate raw rows before
   curated-universe filtering; foreign/untracked rows still count toward collection.
3. Require accounted-for rows and an explicit terminal-page check. A short page,
   empty DataFrame, or successful HTTP status alone is insufficient. For total zero,
   require a valid empty raw result with correct scope/schema.
4. Reject overfull pages, unexplained cross-page duplicate keys, conflicting rows,
   premature empty pages, repeated pages, changing totals and unresolved count
   mismatches as incomplete. The observed 101-row anomaly must not advance a
   guessed offset. Retry an entire affected slice within bounded budgets.
5. Repeat a completed slice once and compare normalized source event sets and
   totals. Disagreement means unstable/incomplete collection. Repetition is a
   conservative consistency check, not a claim of provider snapshot isolation.
6. Account for upper-bound midnight rows before filtering; collapse identical
   overlap between adjacent slices. Keep only dates in the requested inclusive
   market-local interval. Validate all slices before returning `complete=True`.

This is deliberately stricter than naive offset enumeration. If Yahoo's totals
count distinct event categories that produce identical visible rows, establish
that semantics with source/fixture evidence before changing the counting rule.
Do not normalize away a discrepancy merely to obtain a successful refresh.

Initial configurable bounds to validate during implementation:

| Setting | Initial value / purpose |
| --- | --- |
| `YAHOO_HTTP_TIMEOUT_SECONDS` | 20; enforce at the session boundary |
| `YAHOO_MIN_REQUEST_INTERVAL_SECONDS` | 1; local conservative pacing, not a Yahoo limit guarantee |
| `YAHOO_PAGE_SIZE` | 100; validate 1–100 |
| `YAHOO_MAX_PAGES_PER_SLICE` | 20; bound each enumeration pass |
| `YAHOO_MAX_HTTP_ATTEMPTS` | 300; include data/authentication/retry attempts per fetch |
| `YAHOO_MAX_RAW_ROWS` | 30000; bound source rows across collection/verification |
| `YAHOO_MAX_FETCH_SECONDS` | 600; monotonic aggregate deadline per fetch |
| `YAHOO_CACHE_DIR` | Private runtime directory below `/tmp`; cookie/timezone caches |

Use bounded request retries and at most one bounded slice restart inside the
aggregate fetch deadline. Recommend `CALENDAR_PROVIDER_ATTEMPTS=1` for Yahoo to
avoid multiplying a long full collection by outer retries; ordinary runtime
refresh retry remains available. Validate configured fetch/outer-retry timing
against runtime stall settings, with margin for repository calls. A deadline
must prevent new requests and bound individual request timeouts; cancelling an
async wait cannot kill a blocking HTTP call.

Handle rate-limit responses with finite provider-specific backoff/Retry-After and
cooldown, bounded by the aggregate budget. Stop instead of attempting access
workarounds. Sanitize exceptions and suppress yfinance wire/debug logging so
cookies, crumbs, authentication-bearing URLs and bodies cannot escape.

### Step 3 — Normalize observations and apply safe replacements

Normalize only the fields needed for scheduling:

- Map Yahoo symbols to approved enabled CIKs using canonical curated tickers.
  Reject ambiguous mappings. Do not guess aliases from company names; an
  unsupported symbol mapping appears in coverage diagnostics.
- Convert aware timestamps to the market timezone before taking the date.
  Reject malformed/naive timestamps rather than inventing dates.
- Map BMO to `before_market`, AMC to `after_market`, and TNS/TAS/missing or
  unfamiliar timing codes to `unknown`. Retain a bounded diagnostic timing code;
  never infer scheduling precision from the timestamp.
- Preserve a bounded source title and observation provenance. Drop EPS, revenue,
  market cap and unrelated financial values from domain events/diagnostics.
- Keep source-provided timestamps/IDs null when absent. The service retains its
  durable monotonic local `observed_at` as the canonical sync timestamp.

Evaluate the retained titles and pinned source for a narrow fiscal-announcement
grammar, such as `Q3 2026 Earnings Announcement`. If defensible, persist a
versioned normalized hint identifying announcement kind, fiscal year and quarter.
This is matching evidence, not a stable Yahoo ID or independently verified fiscal
period. Missing, unrecognized or ambiguous titles provide no replacement evidence.
If semantics cannot be validated, preserve both dates and log the ambiguity;
do not enable a broader same-CIK/date-proximity heuristic.

Replacement rules:

1. Require same provider, CIK and validated semantic evidence on both observations.
   Match only one old event to one incoming event with a different date.
2. If both dates are present in the incoming collection, or multiple old/new rows
   match, retain them and report ambiguity. A different fiscal event never replaces
   every event for that CIK.
3. An old row can be cancelled only if the new date lies in confirmed collection
   scope. If the new date is beyond the lookahead and was not collected, retain
   the old row. Do not add an unbounded per-ticker search for possible moves.
4. Read existing events over a bounded reconciliation range that includes still
   relevant past window/grace dates and the incoming lookahead. This allows an
   incoming replacement to match an old event that just left the lookahead start.
   Respect the repository maximum range; records outside the range stay untouched.
5. Persist incoming rows first, then strongly confirm each replacement's canonical
   stored value before cancellation. A stale upsert being ignored does not prove
   the intended replacement was stored.
6. Guard cancellation against changes to the exact old observation used to match,
   not merely its provider/date and a timestamp older than the run. On a race,
   reread and recompute or preserve; never cancel a newly changed fiscal event.
7. Keep tombstones and monotonic observation guards. Repair lost acknowledgments
   idempotently; complete sync only after all required writes/confirmations.

Full snapshots are validated before writes, but repository application remains
non-atomic. A crash may temporarily expose both dates; a later valid refresh
repairs the replacement without claiming batch atomicity. A failed database apply
does not advance success. Provider fetch failure leaves expectations unchanged.

### Step 4 — Wire configuration, lifecycle, refresh and diagnostics

Update `main.py` with explicit placeholder/Yahoo factories, validating all provider
settings before AWS construction. Imports, configuration parsing, `--help` and
`--health-check` must not initialize provider clients, caches or network access.

Give Yahoo a dedicated single-worker `BlockingExecution` and owned session/cache
lifecycle; do not share the SEC executor or request limiter. Initialize yfinance's
process-wide cache location before first use, keep one Yahoo owner, and close the
session only after active blocking work has finished. Add a stop/admission signal
so shutdown prevents subsequent Yahoo page/auth/retry requests, including requests
waiting for pacing. Cancellation must retain execution admission through in-flight
I/O and avoid recording an ordinary provider failure for shutdown.

Use a shared market-local date policy for Yahoo full/near-term ranges and runtime
coverage health. Preserve UTC instants for observation and completion timestamps.
Use 30 days as the new lookahead default; retain daily full refresh and near-term
zero. Keep placeholder as the default provider so selection remains explicit.

Distinguish successful collection's declared coverage from current daily cadence:
at restart, an unexpired daily success for the same company scope covering today's
scheduling needs should not refetch solely because the rolling future end advanced
by one day. Refresh when cadence expires, the company scope changes, or usable
coverage is absent. Health must still expose actual coverage and last-success age;
do not claim the uncollected new final day was covered. Test this behavior through
midnight/DST and stale or changed universes. Successful refresh already requests
a durable scheduler reload; reuse that path.

Keep satisfaction independent. With Yahoo's null stable ID, date moves produce new
date-based satisfaction identities; do not transfer an old checkpoint to the new
date. Exact previously satisfied events stay satisfied through refresh/restart;
overlapping ambiguous expectations continue to use existing conservative policy.

Add bounded operational counts for collection attempts/pages, requested/matched/
missing companies, timing coverage, preserved omissions, replacements and matching
ambiguities. Missing companies mean no observation in the horizon, not proven
provider failure or cancellation. Use existing bounded Service/Environment metric
dimensions; no ticker/CIK metric dimensions or unbounded payload logging.

Expose explicit calendar provider/lookahead/cadence/cache configuration in CDK
context and task environment, defaulting to placeholder and stopped service.
Document the Yahoo selection path. No new AWS services/permissions or credential
secrets are implied; continue to synthesize and test offline. Consult the installed
AWS CDK guidance if modifying CDK configuration during implementation.

### Step 5 — Validate failure, restart and container behavior

Add synthetic raw-page fixtures and focused Yahoo tests. Required coverage:

| Area | Required cases |
| --- | --- |
| Collection | Multi-page and exact-page totals; terminal evidence; valid zero; wrong criteria/schema; early empty page; changing total; repeated/overfull pages; duplicate/conflicting rows; unstable verification; caps/deadline exhaustion |
| Dates | D through D+29; following-midnight upper bound; boundary overlaps; timezone conversion; both DST transitions; UTC/New York midnight difference |
| Mapping/timing | Canonical CIK/ticker; untracked symbols; ambiguous mapping; BMO/AMC/TNS/TAS/missing/new codes; nullable/nonfinite values; safe diagnostics |
| Reconciliation | Absence preservation; explicit move; distinct quarters; multiple same-period candidates; both dates present; missing title; out-of-range new date; past/grace old date; same-date schema conflict |
| Durability | Replacement confirmed before cancel; stale writes; changed old/replacement rows; lost upsert/cancel/success acknowledgments; interrupted application; restart; disabled/other-provider protection |
| Runtime | Completion-based daily cadence; restart across midnight; changed company scope; no near-term refresh; reload after success; satisfaction unchanged for same date and independent after move; window/grace expiry |
| Lifecycle | Event loop responsive; independent Yahoo/SEC pacing; finite authentication retries; stop during pacing/pagination/HTTP; repeated cancellation; client/cache cleanup; secret-safe errors |
| Container/CDK | Python 3.12 ARM64 imports/dependency check; writable private caches with read-only root/non-root user; installed Yahoo factory with fake HTTP boundary; network-disabled lifecycle; explicit provider environment; desired count zero |

Retain all existing SEC/acquisition tests and authoritative fixture reconciliation
tests. Extend real SDK Stubber/stateful repository tests for any conditional-write
or serialization changes. Update dependency assertions in the image harness
deliberately; the legacy namespace/PyArrow/test-fake exclusions remain relevant.

Validation commands during implementation, from this repository:

```bash
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m pytest -q
.venv/bin/python -m build
infra/cdk/.venv/bin/python -m pytest -q infra/cdk/tests
npm run synth -- --no-lookups
docker build --platform linux/arm64 -t finbot-ingestion:yahoo-calendar .
FINBOT_CONTAINER_TESTS=1 FINBOT_CONTAINER_IMAGE=finbot-ingestion:yahoo-calendar \
  .venv/bin/python -m pytest -q tests/integration/test_phase7.py -m container
git diff --check
```

Run focused new cases first, then the full application suite, affected CDK/workflow
checks, fresh wheel installation and rebuilt image checks. All routine tests remain
offline. Record actual results and limitations; do not reuse historical counts as
fresh verification. Planning ran no application tests; delivery results are below.

### Step 6 — Document delivery and leave a usable handoff

Update README, `.env.example`, HLD/LLD, the new ADR, deployment instructions and
migration status with the delivered factory, configuration, cancellation rules,
coverage semantics, dependency rationale and validation results. Preserve existing
user edits; at planning time `docs/MIGRATION_PLAN.md` was already modified.

Acceptance requires a real installed Yahoo adapter and proven offline durable
refresh/replacement behavior, rather than only mocked normalized provider events.
Keep collection consistency and production-universe coverage claims distinct.

After offline delivery, the remaining external milestones are the authoritative
company universe, the ongoing provider access arrangement already identified in
the migration handoff, a separately scoped full 30-day live validation, and
separately authorized manual deployment/activation. Do not label the earlier
capped 90-day probe or six-company sample as completion of those milestones.

## 4. Completion criteria

- [x] Contracts/ADR distinguish collection completeness from cancellation authority.
- [x] Yahoo client collects bounded, validated, fresh source pages and returns typed
  events without blocking the scheduler or sharing the SEC request budget.
- [x] Missing/incomplete fetches preserve expectations and successful freshness.
- [x] Validated unique replacements are durable before guarded cancellation;
  ambiguous observations remain active independently.
- [x] Daily market-date refresh, restart coverage, reload, satisfaction and shutdown
  behavior pass offline tests.
- [x] Application, affected CDK/workflow, distribution and ARM64 container checks
  pass with documented dependency/cache changes.
- [x] Documentation records actual delivered behavior and remaining external inputs.

## 5. Delivered implementation

The provider/client/parser/config modules are installed under
`finbot_ingestion.calendar.providers`. Factory selection, explicit reconciliation
policies, additive replacement evidence, strong confirmation/guarded cancellation,
market-date cadence/health and owner-thread cleanup are delivered. README, HLD/LLD,
ADR 009, deployment configuration and migration delivery status describe current
behavior. The original user-edited migration handoff and probe evidence are preserved.

### Implementation decisions resolved during development

- The public Calendars dataframe drops raw totals and cannot establish a correctly
  shaped empty collection. The permitted fallback is used: an owned, pinned
  `YfData.post` instance retains raw source metadata while reusing yfinance auth.
  No global HTTP monkeypatch or cached endpoint result is used. Installed-seam tests
  exercise actual yfinance auth with synthetic responses at the native boundary.
- Repeated enumeration, terminal checks and one slice restart share aggregate
  request/row/deadline bounds. A source anomaly causes an incomplete refresh rather
  than guessed pagination. Query/schema/count evidence remains distinct from
  independent company coverage and source snapshot isolation.
- Replacement matching excludes existing incoming keys from old candidates. A new
  complete refresh therefore repairs interruption after replacement upsert, before
  old-row cancellation or success commit. Exact old-row equality protects a changed
  observation; a stale/ignored replacement write cannot authorize cancellation.
- The source-shaped JSON fixture is synthetic and reusable in unit, durable SDK
  and installed-container tests. Python socket guards are supplemented with a
  native curl guard; production includes no test-mode configuration.
- yfinance brings lxml and curl_cffi into the acquisition-only image deliberately.
  PyArrow, the legacy namespace, test tools and extraction remain excluded.

### Verification results (2026-10-09)

- Compileall passed for source/tests/scripts.
- Full application suite: **555 passed, 8 skipped**; skipped cases are opt-in Docker.
  Final tests include finite authentication retries, secret-safe failures and unsafe
  redirect rejection before another native request.
- CDK/workflow suite: **16 passed**; strict credential-free synthesis without AWS
  lookups passed and service desired count remains zero.
- Source distribution and wheel build passed with isolated build dependencies.
- Fresh wheel installation in an isolated temporary Python 3.14 environment passed;
  installed provider imports, CLI help and `pip check` passed without runtime calls.
- Rebuilt Python 3.12 Linux ARM64 image: **8 passed, 9 deselected** in the opted-in
  container run. These checks ran against the final application code, non-root/read-only
  image with private writable caches and network disabled.
- actionlint passed; the network-disabled non-root heartbeat-volume check passed.

No live Yahoo, SEC or AWS operation, resource deployment, production universe
selection/seeding, GitHub delivery, shared-data mutation or activation occurred.
Remaining milestones are unchanged: approved universe and ongoing access, full
30-day live coverage validation, and separately authorized deployment/activation.
