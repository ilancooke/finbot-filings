# Phase 7 implementation plan — runtime cutover and package cleanup

Status: IMPLEMENTED (2026-10-08).

Phase 7 is delivered. Current supported commands are in README; package/container
contracts are in LLD section 2.7 and completion status in MIGRATION_PLAN. The
original implementation plan is retained below as a historical planning record.

## Delivered scope and validation

- Verified all 48 removed tracked legacy files against recovery revision
  `59cacd78a1da01d00b913c2e67185b0a0980d7ce` before deletion. The only preexisting
  uncommitted work was the Phase 7 planning documents, which were preserved.
  README documents recovery in a separate detached worktree.
- Retained all 439 ingestion tests. Existing tests already cover the necessary
  acquisition regressions; no interpretation tests needed migration. Removed the
  144 legacy tests and replaced root legacy fixtures with offline environment guards.
- Restricted package discovery to `finbot_ingestion*`; removed the legacy console
  entry point, PyArrow/lxml direct dependencies and superseded docs/scripts/tickers.
  Distribution name/version remain `finbot-filings` / `0.2.0`. Added development
  build tooling. No production application/domain/event/storage changes were needed.
  Refreshed the repo-local editable/dev installation to match package metadata and
  provide the build tool; the existing environment was preserved, and removed
  runtime dependencies were not manually uninstalled from it. Clean-environment
  validation establishes the supported dependency boundary.
- Added a two-stage wheel-based Python 3.12 slim image, non-root PID 1 module entry
  point, heartbeat health check and source-only build-context allowlist. Timezone
  data resolves through exchange-calendars' existing dependency. Production image
  tests prove legacy/test namespaces and PyArrow/lxml/pytest are absent.
- Extracted existing stateful SDK fakes for reuse. A mounted-only `sitecustomize`
  shim blocks network and substitutes HTTP/SDK boundaries while retaining real
  configuration, runtime factory, limiter, repositories, adapters and supervision.
  Test containers use no network, dummy credentials, read-only roots and tmpfs.
- Updated current docs/environment examples and preserved previous phase records
  as history. No shared-data operations, live AWS/SEC/provider calls, resource
  mutations, downstream relocation or Phase 8 work occurred.

Executed repository checks:

```bash
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m pytest -q
git diff --check
.venv/bin/python -m build
docker build -t finbot-ingestion:phase7 .
FINBOT_CONTAINER_TESTS=1 .venv/bin/python -m pytest -q tests/integration/test_phase7.py -m container
```

Results: pre-cutover baseline 583 passed (15.52 seconds); final ordinary suite
448 passed with six opt-in container cases skipped (16.92 seconds), including nine
new guarded subprocess cases. All six Docker cases passed against the final rebuilt
image (24.76 seconds), verifying production installation/entry point/session/dependencies,
SIGTERM/SIGINT, four original-byte artifacts and published checkpoints, local and
Docker health, blocked I/O beyond the drain grace, all-client cleanup and required
loop failure/unexpected return. Repository-local Python is 3.14.8; Docker validates
Python 3.12 on Linux/arm64. Initial harness mount/reporting errors were fixed before
the passing run; no production runtime correction was required.

Final local image tag: `finbot-ingestion:phase7`; image ID
`sha256:e5bc3b273c80b841d2e78ea3a5914154237e70d8c72bc8250897b5eb65c7c42e`.
Deployment to another CPU architecture requires building/validating that target
image during Phase 8; this run does not claim Linux/amd64 validation.

Built both sdist and wheel, inspected archive contents/dependencies and installed
the wheel into a fresh temporary virtual environment outside the source tree.
`pip check`, all ingestion module imports, absent legacy/removed dependencies,
module help and missing-heartbeat exit status passed. Child processes use the
test-only network guard. The installation workflow was:

```bash
validation_root=$(mktemp -d /private/tmp/finbot-phase7-validation.XXXXXX)
.venv/bin/python -m venv "$validation_root/venv"
"$validation_root/venv/bin/python" -m pip install dist/finbot_filings-0.2.0-py3-none-any.whl
"$validation_root/venv/bin/python" -m pip check
```

Imports/help/health were then run from that temporary directory against the
installed wheel, with only `tests/container` supplied as PYTHONPATH to install the
network guard. Image builds may download Python/package dependencies; lifecycle
tests themselves are offline. Live calendar/provider and production universe
remain unresolved, and none of these checks claim production deployment readiness.

## Original plan

Prepared against clean `main` at
`59cacd78a1da01d00b913c2e67185b0a0980d7ce` (`Implement phase 6`). This plan
supplements HLD, LLD, accepted ADRs and MIGRATION_PLAN. The original request
authorized planning; implementation was subsequently authorized separately.
Cloud deployment remains outside Phase 7.

## Intended result

Ship one installable ingestion namespace, `finbot_ingestion`, and a container
whose supported entry point is `python -m finbot_ingestion.main`. Remove the
superseded local acquisition/extraction workflows after preserving recoverable
source and validating their replacement. Keep the distribution/repository name
`finbot-filings`; renaming is unnecessary for this phase.

Retain the existing acquisition contracts, shared SEC budget, repositories,
runtime scheduling, placeholder provider and health behavior. Phase 7 changes
packaging, container execution, operational documentation and obsolete code.
Fix runtime issues only when cutover validation demonstrates a concrete need.

CDK, ECR/ECS deployment, GitHub Actions delivery, deployed CloudWatch alarms,
live provider selection, production universe seeding, operator redrive and
completed-package rechecking remain outside this phase. Shared workspace data
is preserved, and interpretation code is not relocated to another repository.

## Findings from the current package

- No tracked Dockerfile, Docker ignore file, Compose configuration or application
  delivery workflows exist.
- `main.py` already supplies runtime wiring, SIGTERM/SIGINT handling, sanitized
  failure exit and `--health-check`; no replacement CLI framework is needed.
- `pyproject.toml` discovers both namespaces and exposes the legacy
  `finbot-filings` console command. Its description still advertises coexistence.
- `pyarrow` and `lxml` are direct legacy dependencies. Ingestion package HTML
  parsing uses BeautifulSoup with the standard-library `html.parser`.
- `exchange-calendars` remains necessary for real XNYS sessions. Its installed
  version, 4.13.2, depends on NumPy, pandas and timezone/calendar helpers. These
  are justified transitive dependencies; removing PyArrow does not mean removing
  pandas or rewriting exchange-session logic.
- `tests/conftest.py` imports legacy models at collection time. Deleting legacy
  source alone would break the ingestion tests even though they do not use those
  fixtures.
- Twelve root `tests/test_*.py` files contain the 144 legacy tests. The unit,
  integration and replay directories contain 439 ingestion tests.
- README and `.env.example` retain coexistence/local-workflow instructions.
  `scripts/download_tickers.sh` and `tickers.txt` belong to the legacy workflow.

## Implementation sequence

### 1. Preserve the pre-cutover package and establish the baseline

Recheck Git status before edits. Record the complete pre-cutover commit in the
cutover notes, together with a recovery command that checks it out in a separate
worktree. The clean inspected revision above already contains the legacy source,
tests, fixtures and docs. Ordinary deletion commits preserve it in history;
there is no need to keep an archive subtree in the supported package.

If user edits appear before implementation, preserve their exact diff and any
untracked source/test artifacts in a verified local recovery copy before removing
affected files. Do not reset, silently commit, or assume uncommitted changes are
recoverable from the recorded revision. Avoid archiving credentials or `.env`.

Run the current offline suite and compilation before removal. Compare legacy
acquisition guarantees with the existing ingestion tests: original bytes,
duplicate handling, partial enumeration, storage repair, publication retry and
amendment identity. Port any necessary missing acquisition regression before
deleting its old test; interpretation-only regressions remain recoverable in Git.

Deliverable: documented recovery revision, baseline results and an explicit list
of any acquisition regressions that must move into the ingestion suite.

### 2. Remove obsolete code and isolate the remaining tests

Delete `src/finbot_filings/` after the preservation gate. Remove the following
legacy test files and their legacy-only root fixtures:

| Files | Superseded behavior |
| --- | --- |
| `test_acquisition.py`, `test_client.py`, `test_filings.py` | Local acquisition and old SEC interfaces |
| `test_cli.py`, `test_config.py`, `test_layout.py`, `test_storage.py` | Legacy commands, configuration and filesystem bundles |
| `test_parsing.py`, `test_parsing_batch.py` | Section interpretation |
| `test_xbrl_download.py`, `test_xbrl_extract.py`, `test_xbrl_taxonomy.py` | Specialized XBRL acquisition and interpretation |

Replace or remove `tests/conftest.py` so collection no longer imports legacy
modules. Preserve the network blocking and fake-credential discipline of the
remaining unit/integration/replay tests, and apply equivalent protection to new
subprocess tests. Parent-process monkeypatches do not block child-process network
access; the subprocess fixture must install its own guard.

Keep `submissions_mixed.json`, `sec_package/`, and calendar fixtures used by the
ingestion suite. Remove `missing_anchor_10q.html`, `normal_10k.html`,
`normal_10q.html` and `physical_order_10q.html` if a reference check confirms they
remain legacy-only. Inspect tracked files rather than deleting arbitrary cached or
untracked files; an ignored cache is not an additional supported feature.

Remove `scripts/download_tickers.sh`, `tickers.txt`, `README-legacy.md` and
`ROADMAP-legacy.md`. Put their recovery location in the cutover note. Retain prior
phase plans/replay reports as historical records, with a clear pointer to the
current README where historical command examples no longer apply.

Deliverable: an ingestion-only source/test tree with retained acquisition coverage.

### 3. Complete distribution and dependency cleanup

Update `pyproject.toml` to discover only `finbot_ingestion*`, remove the legacy
console entry point, and describe the focused service. Keep the documented module
entry point and existing `--help`/`--health-check`; do not add an alias or a command
to seed, provision, redrive or mutate operational state.

Remove direct `pyarrow` and `lxml` dependencies. Retain Boto3, requests,
BeautifulSoup and exchange-calendars with their existing justified constraints.
Keep the declared Python support of `>=3.12`; validate Python 3.12 in the Linux
container and record local Python 3.14 validation separately. Add build tooling
to development dependencies only if needed by the artifact validation workflow.

Build a wheel and sdist. Install the wheel into a fresh environment outside the
source tree, then run import, module help and local health checks. Inspect the
distribution contents and metadata: no legacy namespace/console entry point,
PyArrow requirement, bundled operational data or credentials. Verify `pip check`.
An existing editable `.venv` is not evidence that the clean distribution works.

### 4. Add the production container

Add a Dockerfile based on a Python 3.12 slim Linux image. Use a wheel build stage
and install the wheel plus runtime dependencies into the final image. Include
timezone data needed by the existing exchange-calendar adapter. Validate the
actual dependency resolution and session lookup in this image.

Run under a dedicated non-root user. Use an exec-form entry point equivalent to:

```text
["python", "-m", "finbot_ingestion.main"]
```

Python receives container signals directly. Set unbuffered output and disable
bytecode writes. Use the existing writable `/tmp` heartbeat location; no shared
data volume, published port or `.env` loader is needed. The final image contains
the installed application and runtime dependencies, not pytest or lifecycle fakes.

Add a local health check using the same module's `--health-check` argument. Initial
Docker defaults: 30-second interval, 5-second timeout, 120-second startup allowance
and three retries. These are container settings, not new scheduling policy. Test
that startup allowance covers the fixture bootstrap. A stale or unconfigured
calendar remains visible in the heartbeat but does not redefine process liveness.
Document that ECS health configuration is separate Phase 8 work.

Add `.dockerignore` excluding `.git`, `.venv`, `.env` and other credential files,
local data, caches, tests and build outputs from the production build context;
retain `.env.example` only if the build needs it. Copy an explicit application
allowlist into build/final stages. Extend `.gitignore` for new build artifacts.
Do not modify or delete the user's ignored `.env` or installed virtual environment.

Document a stop timeout longer than the configured queue-drain grace for local
Docker runs. The 30-second application grace does not cap blocking HTTP/SDK cleanup;
forced termination still relies on durable checkpoints. A container build is not
a production deployment, and no live resources are needed to validate the image.

### 5. Verify entry point and container lifecycle against fake boundaries

Add focused tests under `tests/integration/` and a test-only fixture/harness under
`tests/container/` or `tests/fixtures/`. Reuse existing stateful SEC/SDK fakes and
RuntimeApplication rather than implementing another ingestion service.

Use a mounted test-only startup shim to install fake boundary constructors before
executing the real runtime module in the same Python process. Preserve the real
configuration parsing, application construction, signal handling and supervision.
Fakes and their activation mechanism must stay outside the production wheel/image;
do not add a production environment variable that switches the service to mock mode.

Run container lifecycle cases with `--network none`, dummy AWS credentials,
metadata lookup disabled, and temporary fixture state. Do not mount the real
shared data root or AWS credentials. Docker-dependent tests are explicit opt-in
and may skip with a clear reason in ordinary offline pytest; their successful
execution remains required to declare Phase 7 complete.

| Case | Required evidence |
| --- | --- |
| Production image contract | Default entry point executes module; non-root user; help works; installed runtime imports and XNYS session lookup succeed |
| Invalid startup configuration | Production entry point exits nonzero before any live call; sanitized error |
| Local health command | Missing, malformed, stale and unhealthy heartbeats fail; fresh live heartbeat succeeds without AWS/SEC settings |
| Fixture runtime startup | Real runtime creates bounded workers/queues and a live heartbeat; placeholder state is visibly degraded |
| OS SIGTERM and SIGINT | Actual process signal stops admissions, permits cleanup, removes heartbeat and exits successfully |
| Blocking work during shutdown | In-flight fake blocking call is awaited before client/executor close; stop timeout permits it |
| Required-loop failure | Real supervision exits nonzero and cannot leave a healthy idle process |
| Artifact path | Fake SEC bytes traverse enumeration, conditional storage, stored checkpoint, publication and published checkpoint |

Add installed-package subprocess checks where these expose behavior beyond the
existing in-process Phase 6 tests. Keep failure/restart suites from Phases 3–6;
they already cover durable recovery after interrupted work and lost acknowledgments.
Avoid duplicating every persistence test in Docker. Fix only lifecycle problems
revealed by these checks and rerun affected regressions.

### 6. Update supported workflows and completion records

Rewrite README around ingestion-only installation, exported environment
configuration, module execution, image build/run, local health, signal shutdown,
logs/metrics and terminal-work investigation. Explain the existing-resource and
nonempty-enabled-universe prerequisites. Provide clearly labeled examples; normal
runtime execution contacts AWS/SEC, while help and heartbeat checks do not.

Remove the legacy paths/CIK overrides/coexistence comments from `.env.example`.
Retain the runtime settings and `SEC_USER_AGENT` requirement, with resource names
as examples and no secrets. Docker may receive an explicit `--env-file`; the
application itself continues to read exported process variables only.

Update LLD implementation/container notes and MIGRATION_PLAN with delivered files,
the recovery revision, actual validation commands/counts and remaining limitations.
Correct current-tense coexistence claims; preserve clearly identified historical
phase records. Link this plan from the Phase 7 migration section. HLD/ADRs change
only if a material architecture change is actually required.

Mark Phase 7 complete only after all acceptance gates below pass. Identify
Phase 8 as the next milestone without starting it. A packaged runtime is distinct
from production readiness: live calendar access and the production universe are
still external inputs.

## Validation and completion gates

Run repository-local compilation and the remaining offline suite:

```bash
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m pytest -q
git diff --check
```

Also build/install the distribution in a fresh environment, run `pip check`,
inspect its contents/dependencies, rebuild the image from final source, and run
the explicit offline container lifecycle suite. Record those exact commands once
the harness/image name is implemented.

Completion requires all of the following:

- The recorded pre-cutover revision/recovery copy contains every removed piece
  of useful legacy work, including any new user edits.
- All retained ingestion tests and necessary migrated acquisition regressions pass.
  A lower count after deliberate removal of 144 legacy tests is expected.
- Fresh package/image installation succeeds without the legacy namespace or
  PyArrow/lxml direct requirements. No runtime dependency on local data roots,
  hashes, legacy interpretation or overwrite flags remains.
- Module entry point, health, Linux/non-root execution, real process signals and
  failure exits pass against offline fake boundaries in the rebuilt image.
- README, `.env.example`, image configuration and migration status agree.
- Shared data, other repositories and AWS resources remain untouched.

If container execution cannot be completed, report the exact limitation and leave
Phase 7 incomplete rather than substituting static Dockerfile inspection for
lifecycle evidence. Provider selection/universe seeding do not block this mocked
cutover validation, but remain prerequisites for production use.

## Planning validation and environment limits

On 2026-10-08, the inspected clean revision collected 583 tests: 144 legacy and
439 ingestion tests. A fresh baseline run passed all 583 in 15.61 seconds using
the repository-local Python 3.14.8 environment. No implementation edits, image
builds, live SEC/provider operations or AWS changes were performed.

Docker CLI exists at `/usr/local/bin/docker`. Read-only `docker info` could not
connect within the default sandbox because access to the Docker socket was denied.
A subsequent read-only check with approved execution outside the sandbox succeeded
and reported Docker Engine 29.7.2. Docker is available through that execution path;
image builds and lifecycle tests remain to be performed during implementation.
