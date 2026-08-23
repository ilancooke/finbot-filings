# finbot-filings

`finbot-filings` acquires public SEC 10-K and 10-Q filings and turns them into
inspectable document-native sections, normalized XBRL facts, and source-shaped
XBRL label and presentation datasets. It stores the original primary HTML and
SEC-generated XBRL inputs so every derived artifact can be traced back to the
downloaded source. It is a standalone Python 3.12+ package in the Finbot
workspace.

The package intentionally keeps raw acquisition separate from derived section and fact output. Section discovery is driven by the filing's native linked outline; headings cannot independently create an outline and are used only by a constrained repair for a recognized TOC row with a broken target. Taxonomy-driven canonical fact selection, ratio calculation, semantic summarization, feature generation, LLMs, databases, AWS, and S3 remain out of scope.

## Workflow overview

```text
SEC ticker map + submissions metadata
  -> discover                     inspect filing metadata without writing files
  -> download                     acquire one complete raw filing bundle
       |
       +-> parse-sections          write native section text + manifest
       |
       +-> inspect-xbrl            validate and inventory raw XBRL inputs
       +-> inventory-taxonomy      write a local taxonomy inventory manifest
       +-> extract-taxonomy        write label + presentation Parquet tables
       +-> extract-xbrl            write normalized facts.parquet + metadata
              -> show-xbrl         inspect or export selected normalized facts
```

The workflows have distinct responsibilities:

- **Discovery** resolves a ticker to its SEC CIK and finds exact-form filings from official SEC submissions metadata. It is read-only; `download` performs the same discovery internally before acquisition.
- **Acquisition** writes durable raw inputs for each accession: primary filing HTML, filing metadata, and SEC XBRL package/instance files when available. `download-xbrl` is a maintenance workflow for backfilling or repairing XBRL inputs on filings already stored locally.
- **Section extraction** uses the filing's own top-level outline to divide the large HTML document into complete native sections. Canonical IDs and semantic categories are optional routing annotations; they do not control whether source text is retained.
- **XBRL extraction** inventories every fact in the SEC-generated instance document and writes a versioned Parquet dataset without choosing preferred accounting concepts or calculating ratios.
- **Taxonomy inventory** securely discovers local schemas and standalone or embedded linkbases, records roles and relationship counts, and identifies external dependencies without fetching them.
- **Taxonomy extraction** materializes every filed label resource and
  presentation relationship into versioned Parquet tables, retaining source
  roles, endpoint URIs, ordering, attributes, resolution status, and diagnostics.
- **Inspection** commands expose filing discovery results, raw XBRL inventory, and normalized facts for human review or downstream development.

Section, fact, and taxonomy extraction are independent branches from the same
raw filing bundle. You can rerun any derived processing without redownloading.
`inventory-taxonomy` is a compact diagnostic workflow, not a prerequisite for
`extract-taxonomy`; both securely read the same local package.

## Design tenets and package boundaries

- **Data completeness and quality are the primary objective; cost reduction is secondary.** Prefer deterministic extraction when it is reliable, but expose difficult cases for targeted LLM or human review rather than silently losing source material.
- **Use official public SEC sources and retain provenance.** Raw HTML, XBRL packages, generated instances, SEC URLs, accessions, and hashes remain the audit trail for every derived artifact. This package does not use filing-submission APIs, search-result scraping, or third-party EDGAR services.
- **Preserve the filing before classifying it.** Native section titles and filer-defined XBRL structures are durable outputs. Canonical IDs, semantic categories, and statement-kind candidates are optional routing aids and never determine whether source content is kept.
- **Use narrative and structured sources together.** Native HTML sections are the appropriate input for narrative analysis. XBRL facts and taxonomy relationships are the preferred source for exact financial values when available.
- **Do not optimize for one filing template or taxonomy year.** Parsers must retain unfamiliar sections, roles, concepts, namespaces, and relationships; support valid packaging variations; and report uncertainty explicitly.
- **Defer model-specific work.** This package does not further split native sections, decide which evidence a feature needs, select final accounting concepts or contexts, calculate ratios, or invoke an LLM.

The responsibility boundary is concise: `finbot-filings` describes **what a
filing contains and how the filer organized it**; a downstream feature package
decides **what that evidence means for a particular feature**. Downstream results
should record the section IDs, statement roles, concepts, fact contexts, and
source ranges used in every calculation or model call.

See [ROADMAP.md](ROADMAP.md) for planned official-taxonomy resolution, logical
statement inventory, downstream retrieval contracts, and the rationale behind
their sequencing.

### Typical end-to-end run

Acquire three annual filings for one company, derive their sections, XBRL facts,
and taxonomy tables, and inspect selected facts:

```bash
.venv/bin/finbot-filings download AAPL --form 10-K --count 3
.venv/bin/finbot-filings parse-sections --form 10-K
.venv/bin/finbot-filings extract-xbrl AAPL --form 10-K
.venv/bin/finbot-filings extract-taxonomy AAPL --form 10-K
.venv/bin/finbot-filings show-xbrl AAPL --form 10-K --concept Assets
```

`parse-sections` scans every locally downloaded filing matching the form;
`extract-xbrl` and `extract-taxonomy` can target one ticker as shown or omit the
ticker to process every matching local filing. Derived workflows skip current
outputs unless `--overwrite` is supplied.

## Installation

```bash
cd repos/finbot-filings
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

## SEC identification

The SEC asks automated clients to identify themselves. Copy the example configuration and edit `.env` with your real download location and a truthful organization/application name plus monitored contact address:

```bash
cp .env.example .env
```

```dotenv
DOWNLOAD_FOLDER=/absolute/path/to/finbot/data/raw/filings/
SECTION_FOLDER=/absolute/path/to/finbot/data/filings/sections/
XBRL_FOLDER=/absolute/path/to/finbot/data/filings/xbrl/
SEC_USER_AGENT=Finbot your-email@example.com
SEC_CIK_OVERRIDES=EXAMPLE:123456
```

Replace the download folder, section folder, XBRL folder, and User-Agent dummy values with real values. Remove the optional example CIK override unless you need a ticker-continuity override. The package reads `SEC_USER_AGENT` from `.env` automatically. A process environment variable with the same name takes precedence. The package has no fabricated fallback identity and exits with a configuration error if this setting is absent. Requests use a 30-second timeout and are limited to at most 10 starts per second by default.

## Discovery

```bash
finbot-filings discover AAPL --form 10-K --count 3
finbot-filings discover JPM --form 10-Q --count 3
```

Ticker matching is case-insensitive. Only exact `10-K` and `10-Q` forms are supported, so amendments such as `10-K/A` and `10-Q/A` are excluded. The default count is 5, and count must be positive.

> Filings are ordered by SEC filing date descending. The report period does not determine recency.

Discovery resolves tickers with the SEC-maintained `company_tickers.json` file and reads filing metadata from the company's SEC submissions JSON. It starts with `filings.recent`. When that data contains fewer exact-form matches than requested, it loads the SEC-referenced `filings.files` metadata newest-first until the requested count is satisfied or history is exhausted. Combined records are deduplicated by accession number and ordered globally by filing date.

SEC ticker ownership can move to a successor CIK after a corporate reorganization while older filings remain under the predecessor CIK. Optional continuity overrides use a comma-separated `TICKER:CIK` setting, for example `SEC_CIK_OVERRIDES=XOM:34088`. The ticker must still exist in the official SEC ticker map; the override only selects which official SEC submissions history to query.

## Download

```bash
finbot-filings download AAPL --form 10-K --count 3
finbot-filings download AAPL --form 10-K --count 3 --overwrite
```

The package-level `.env` configures the download directory and SEC identity:

```dotenv
DOWNLOAD_FOLDER=/Users/ilan/workspace/finbot/data/raw/filings/
SECTION_FOLDER=/Users/ilan/workspace/finbot/data/filings/sections/
XBRL_FOLDER=/Users/ilan/workspace/finbot/data/filings/xbrl/
SEC_USER_AGENT=Finbot your-real-email@example.com
SEC_CIK_OVERRIDES=XOM:34088
```

Process environment variables take precedence over the config file. `--download-folder PATH` can override the configured download path for one command. `download` acquires a complete filing bundle when SEC XBRL inputs are available: it downloads the accession ZIP and generated instance, copies the ZIP member named by SEC `primaryDocument` to `filing.html`, and retains the original ZIP. This avoids a redundant primary-document request. If no XBRL package exists or the package omits the primary member, it falls back to the official primary-document URL.

A complete existing HTML/XBRL bundle is skipped by default. Partial local state is repaired: existing HTML is preserved while missing XBRL inputs are backfilled, and missing HTML can be recovered from an existing local package. `--overwrite` explicitly reacquires and replaces the complete bundle. `.env` is ignored by Git; `.env.example` contains only dummy values and is safe to commit.

Files use this predictable layout:

```text
/Users/ilan/workspace/finbot/data/raw/filings/
└── AAPL/
    ├── 10-K/
    │   └── 0000320193-25-000079/
    │       ├── filing.html
    │       ├── metadata.json
    │       └── xbrl/
    │           ├── package.zip
    │           ├── instance.xml
    │           └── metadata.json
    └── 10-Q/
        └── 0000320193-26-000020/
            ├── filing.html
            ├── metadata.json
            └── xbrl/
                ├── package.zip
                ├── instance.xml
                └── metadata.json
```

For XBRL-enabled filings, `filing.html` contains the exact primary-document member bytes from `package.zip`. Otherwise it contains the exact primary-document response bytes. `metadata.json` records the company, CIK, form, filing and report dates, accession number, SEC URLs, primary filename, acquisition method, optional package member, and UTC download time.

Only the `TICKER/FORM/ACCESSION` hierarchy is read. The earlier
`TICKER/ACCESSION` layout is intentionally not migrated or supported; clear the
old sample directories before redownloading if you do not want both layouts on
disk.

### Batch downloads

For a list of companies, create a text file with one ticker per line. Blank lines and comments beginning with `#` are ignored:

```text
# Large-cap examples
AAPL
MSFT
JPM
WMT
```

From the package root, run:

```bash
scripts/download_tickers.sh tickers.txt 10-K 3
```

The script can also be invoked from another directory when both the script and ticker-file paths are adjusted accordingly; it still resolves the package executable and `.env` relative to the script location.

The positional arguments are the ticker file, exact form, and filings per ticker. Form defaults to `10-K` and count defaults to `3`. The script continues when an individual ticker fails, prints a final success/failure summary, and exits nonzero if any ticker failed.

## XBRL package backfill

New `download` operations acquire XBRL automatically. Use `download-xbrl` to backfill filings created by older package versions or to repair only raw XBRL inputs:

```bash
.venv/bin/finbot-filings download-xbrl --form 10-K
.venv/bin/finbot-filings download-xbrl --form 10-Q
.venv/bin/finbot-filings download-xbrl AAPL --form 10-K
```

The ticker is optional. Without one, the command scans every locally stored filing matching `--form` under `DOWNLOAD_FOLDER`. For each filing, it reads the official SEC accession-directory `index.json`, finds the SEC-generated `-xbrl.zip` package and `_htm.xml` instance, and downloads both. It does not scrape filing pages.

Each package is stored beside its primary filing:

```text
data/raw/filings/
└── AAPL/
    └── 10-Q/
        └── 0000320193-26-000020/
            ├── filing.html
            ├── metadata.json
            └── xbrl/
                ├── package.zip
                ├── instance.xml
                └── metadata.json
```

The package contains the Inline XBRL filing and its taxonomy/linkbase resources. The SEC-generated instance is a separate accession-directory file, stored locally as `instance.xml`. Acquisition metadata records both original SEC filenames and URLs, SHA-256 checksums, byte and member counts, and the UTC download time. The archive is validated but preserved unmodified.

Do not manually unzip the archive for normal processing. The taxonomy inventory
and extraction workflows read members directly from `package.zip` so the
extracted files are not duplicated on disk.

Complete package/instance/metadata sets are skipped by default. Existing downloads created before `instance.xml` support are repaired by rerunning the command; `--overwrite` is not required. Use `--overwrite` to download a complete set again, or `--download-folder PATH` to override `DOWNLOAD_FOLDER`. A filing for which the SEC lists no `-xbrl.zip` is reported as `NO-XBRL` and does not fail the batch; invalid metadata, SEC request errors, invalid archives, and invalid instance XML do.

## XBRL taxonomy inventory

Inventory the taxonomy resources already stored in `package.zip`:

```bash
.venv/bin/finbot-filings inventory-taxonomy AAPL --form 10-K
.venv/bin/finbot-filings inventory-taxonomy --form 10-Q
.venv/bin/finbot-filings inventory-taxonomy AAPL --form 10-K \
  --accession 0000320193-25-000079
```

This is a local, no-network workflow. It securely reads XML members directly
from the ZIP, discovers resources by XML content, supports conventional
standalone linkbases and linkbases embedded inside a filing XSD, and inventories
schema imports/includes/redefines, linkbase references, role and arcrole types,
labels, locators, resources, and relationships. External URLs are recorded as
dependencies but are never fetched by XML parsing.

The command writes a compact manifest alongside other derived XBRL output:

```text
data/filings/xbrl/
└── AAPL/
    └── 10-K/
        └── 0000320193-25-000079/
            ├── facts.parquet               # present after extract-xbrl
            ├── metadata.json               # present after extract-xbrl
            └── taxonomy_inventory.json
```

The inventory contains source hashes, resource metadata, schema references,
complete role/arcrole definitions, relationship and label counts, resolution
statuses, external dependencies, security limits, warnings, and failure details.
It intentionally does not duplicate every label or relationship. Use
`extract-taxonomy` to materialize complete label and presentation-relationship
Parquet tables from this validated source graph.

Existing inventories are skipped only when the source package hash, inventory
schema version, and parser version still match. Stale inventories are rebuilt
automatically, while `--overwrite` forces regeneration. `--download-folder` and
`--output-folder` override configured roots. The optional ticker, form, and
accession filters support either targeted or corpus-wide processing. Failures
write a diagnostic manifest when filing identity is available and cause a
nonzero batch exit.

## XBRL label and presentation extraction

Materialize label resources and presentation networks already stored in each
local `package.zip`:

```bash
.venv/bin/finbot-filings extract-taxonomy AAPL --form 10-K
.venv/bin/finbot-filings extract-taxonomy --form 10-Q
.venv/bin/finbot-filings extract-taxonomy AAPL --form 10-K \
  --accession 0000320193-25-000079
```

The command writes an atomic accession-level bundle beneath `XBRL_FOLDER`:

```text
data/filings/xbrl/
└── AAPL/
    └── 10-K/
        └── 0000320193-25-000079/
            ├── concept_labels.parquet
            ├── presentation_roles.parquet
            ├── presentation_relationships.parquet
            └── taxonomy_metadata.json
```

- `concept_labels.parquet` preserves every filed label relationship and exact
  label resource, including language, label role, source XML, arbitrary
  attributes, and explicitly marked orphan resources or locators.
- `presentation_roles.parquet` preserves every presentation extended-link
  occurrence and its filer-defined role definition and `usedOn` declarations
  when locally available.
- `presentation_relationships.parquet` preserves every presentation arc,
  parent/child source references, exact filed order, preferred-label role,
  arbitrary attributes, and endpoint-resolution status.
- `taxonomy_metadata.json` is the completion marker. It records schemas, row and
  source counts, package and parser versions, endpoint coverage, orphan and
  duplicate diagnostics, warnings, and generation time.

The workflow is local and never fetches an external taxonomy. Locally defined
concept endpoints receive authoritative QNames. Standard-taxonomy endpoints
whose schemas are external retain their complete source URI and resolution
status with a null QName; the parser does not guess QNames from fragment text.
Official versioned taxonomy resolution remains a future roadmap milestone.

Output rows are deterministically ordered, source XML numeric attributes remain
exact strings, duplicate arcs are retained, and locator labels are scoped to
their own extended link. Existing bundles are skipped only when all expected
files exist and their source hash, table schema, and parser version are current.
Incomplete or stale bundles rebuild automatically; `--overwrite` forces a
rebuild. The optional ticker, `--form`, `--accession`, `--download-folder`, and
`--output-folder` filters match `inventory-taxonomy`. Filing failures write
diagnostic metadata, processing continues, and any failure makes the batch exit
nonzero.

`[WARNING]` still means the complete output bundle was published. It indicates
retained source diagnostics, such as a presentation locator declared inside an
otherwise empty role but unused by any arc. Inspect `warnings` and the associated
diagnostic records in `taxonomy_metadata.json`; `[FAIL]` is reserved for a filing
whose output bundle could not be completed.

## XBRL fact extraction

Inspect raw XBRL inputs without writing derived data:

```bash
.venv/bin/finbot-filings inspect-xbrl AAPL --form 10-K
```

Normalize one company or every matching local filing into Parquet:

```bash
.venv/bin/finbot-filings extract-xbrl AAPL --form 10-K
.venv/bin/finbot-filings extract-xbrl --form 10-Q
```

The extractor reads `DOWNLOAD_FOLDER` and writes `facts.parquet` plus `metadata.json` beneath `XBRL_FOLDER`:

```text
data/filings/xbrl/
└── AAPL/
    └── 10-K/
        └── 0000320193-25-000079/
            ├── facts.parquet
            └── metadata.json
```

The versioned fact schema captures every standard or filer-extension concept dynamically. It preserves exact value text, concept QName, entity, instant or duration period, unit, explicit or typed dimensions, decimals/precision, nil status, language, document order, and source provenance. Numeric values remain exact strings rather than floating-point values. Equivalent duplicate facts receive stable group IDs and counts but are not discarded or preferred automatically.

Existing complete outputs are skipped unless `--overwrite` is supplied. `--download-folder` and `--output-folder` provide one-command overrides.

Inspect or export normalized facts without converting the durable dataset:

```bash
.venv/bin/finbot-filings show-xbrl AAPL --form 10-K --concept Assets
.venv/bin/finbot-filings show-xbrl AAPL --form 10-K --concept Assets --format json
.venv/bin/finbot-filings show-xbrl AAPL --form 10-K --concept Assets \
  --format csv --output /tmp/aapl-assets.csv
```

`--concept` accepts a local name such as `Assets` or a prefixed name such as `us-gaap:Assets`. Use `--accession` to select one filing and `--limit` to bound diagnostic output. Canonical concept mapping and ratio construction belong in `finbot-features`, where the desired period, dimensions, and accounting meaning are known.

## Deterministic section extraction

Parse all downloaded 10-Q filings with:

```bash
.venv/bin/finbot-filings parse-sections --form 10-Q
```

Parse 10-K filings with:

```bash
.venv/bin/finbot-filings parse-sections --form 10-K
```

The command reads `DOWNLOAD_FOLDER` and writes to `SECTION_FOLDER`. Explicit paths are also supported:

```bash
.venv/bin/finbot-filings parse-sections \
  --input-folder /path/to/raw/filings \
  --output-folder /path/to/filing/sections \
  --form 10-Q \
  --overwrite
```

Existing manifests are skipped unless `--overwrite` is supplied. A batch exits nonzero when at least one processed filing fails.
Run with `--overwrite` once after upgrading so each filing receives a
schema-version 4 section-only manifest with native-outline coverage. Overwrite also removes any `chunks/`
directories produced by the retired schema-version 2 workflow.

### Parsing method

The `native-toc-v2` parser:

1. Locates the filing's primary table-based outline using forward internal links, then reads every row in that selected table so unlinked Part I/Part II markers are retained as structural context. Ordinary Item and note cross-references elsewhere in the filing are excluded.
2. Preserves each outline entry's document-native title, anchor, and physical order. Item labels such as `Item 1 C.` are normalized only for stable IDs and optional mappings.
3. Supports both conventional SEC Item outlines and topic-oriented outlines such as Intel's. Structurally valid native Items are extracted even without a canonical definition; multiple native sections in one row and nonstandard Items such as AAL 10-Q Items 1A/1B and 10-K Items 8A/8B remain separate.
4. Requires each accepted link to resolve to an exact, forward HTML `id` target. When a recognized TOC target is missing, a constrained repair may use a unique exact Item-and-caption heading only when that heading has an ID or is immediately preceded by an empty ID-bearing anchor; the recovery is recorded in diagnostics.
5. Resolves conflicting Item links using destination Item/caption evidence and link-text evidence. A leading `Table of Contents` label at the destination is treated as presentation text, and page-number links do not independently define sections.
6. Sorts resolved targets by physical DOM position and extracts normalized visible text from each target to the next physical target.
7. Adds a canonical SEC section ID only when the Item/Part mapping is deterministic. Otherwise it may add conservative semantic categories while retaining the native section unchanged.

If standard TOC discovery finds no region, a 10-K-only fallback can recognize rows that omit the word `Item`, such as `1 Business`. The fallback activates only when one table contains at least eight distinct canonical sections including Items 1, 1A, 7, and 8; every accepted row must begin with a valid Item token, have a compatible canonical caption, end with a numeric page reference, and link forward to a destination beginning with the exact expected Item heading. Successful use is recorded as `itemless_toc_fallback_used` in diagnostics.

The parser does not use headings to discover a filing outline and does not invoke
an LLM. Exact headings are considered only to repair a missing target from an
already recognized TOC row under the constraints above.
Canonical mapping completeness is not a condition of successful source extraction.

Materially complex filing layouts and possible deterministic or LLM-assisted recovery paths are tracked in [Complex Filing Layouts and Recovery Registry](docs/parser-limitations.md).

### Extraction and mapping status

Source extraction and canonical mapping are reported independently:

- `success` means every recognized native section in the primary outline has a resolved boundary. A broken redundant link fragment remains visible in diagnostics but is nonfatal when another link in the same TOC row resolves that section.
- `partial` means usable native sections were written but one or more recognized sections have no resolvable anchor candidate.
- `failure` means the parser could not establish a credible source outline.
- Canonical mapping is separately `complete`, `partial`, or `none` and never changes a successful source extraction into a failure.

Schema-version 4 manifests also report every detected top-level native outline
entry as extracted or skipped with a reason. Complete extraction requires 100%
native-outline coverage; semantic or canonical mapping completeness is not part
of that decision.

At least six native sections are required for a 10-Q and eight for a 10-K. These
minimums reject incidental link clusters; they do not require a fixed canonical
section inventory.

Stable failure codes include `unknown_form_type`, `no_toc_found`, `no_recognized_item_links`, `anchor_target_missing`, `ambiguous_part_assignment`, `ambiguous_item_links`, `insufficient_section_anchors`, and `parse_error`.

Example output:

```text
[PASS] AAPL/10-Q/0000320193-26-000020/filing.html — 11 native sections; 11 exact mappings; 0 semantic-only
[PASS] INTC/10-K/0000050863-26-000011/filing.html — 24 native sections; 0 exact mappings; 7 semantic-only
[FAIL] EXAMPLE/10-K/0000000000-26-000001/filing.html — no_toc_found

Parsing summary
---------------
Files found:     3
Files processed: 3
Extraction succeeded: 2
Extraction partial:   0
Extraction failed:    1
Skipped:              0
Native extraction rate: 66.7%
Complete files:        2/3 (66.7%)
Native sections:       35
Native outline coverage: 35/35 (100.0%)
Exact mappings:        11
Semantic-only:         7
Unmapped sections:     17

Failure reasons:
  no_toc_found: 1
```

### Section output

Each filing receives an isolated derived-data directory:

```text
data/filings/sections/
└── AAPL/
    └── 10-Q/
        └── 0000320193-26-000020/
            ├── sections/
            │   ├── 01_part1_item1.txt
            │   ├── 02_part1_item2.txt
            │   └── ...
            └── manifest.json
```

Schema-version 4 manifests contain source provenance, parser version, extraction
status, native-outline coverage and row dispositions, native titles and IDs,
anchors, physical ordering, optional canonical IDs, semantic categories,
explicit registrant-caption evidence, and section files.
Failed filings receive a diagnostic manifest but no fabricated text.

Downstream feature code should read the manifest, select relevant native sections
and record exactly which section inputs were sent to a model. Any additional
splitting or model-specific token budgeting belongs in that downstream step.
Registrant roles,
ticker-default policy, feature selection, and LLM calls belong downstream rather
than in this acquisition/extraction package. See [Native Section Architecture](docs/native-section-architecture.md).

## Tests

```bash
.venv/bin/python -m compileall src tests
.venv/bin/python -m pytest
```

The normal test suite uses fake SEC responses and does not require network access.

## Known limitations

- Only exact forms `10-K` and `10-Q` are supported.
- There is no date-range filtering.
- Local filesystem storage is the only storage implementation.
- Section parsing requires a distinguishable TOC region with usable internal anchors. Headings cannot create an outline; they can only repair a recognized missing TOC target under the unique exact-heading rule.
- Some filings contain ambiguous duplicate links, backward-only cross-links, missing anchor targets, or no link-based TOC and will fail deliberately.
- Material without its own top-level outline entry is still retained between adjacent boundaries. Trailing material such as signatures is therefore included in the final native section, and an unlinked or noncontiguous appendix may not be assigned to its semantic owner.
- Noncontiguous canonical Item reconstruction is not attempted; native topic sections remain available instead. Complex cases are documented in [the parser limitations registry](docs/parser-limitations.md).
- Section text is normalized for readability but tables are flattened to text; semantic table reconstruction is not attempted.
- XBRL labels and presentation networks are materialized, but official standard-taxonomy concept metadata, definition/dimensional networks, calculation networks, and ordered statement views are not yet available. Canonical financial-concept selection remains downstream.
- Feature extraction, LLM calls, and semantic summarization are intentionally out of scope.
