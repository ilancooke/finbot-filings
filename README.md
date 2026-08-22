# finbot-filings

`finbot-filings` discovers public SEC 10-K and 10-Q filings, stores each filing's original primary HTML document, and deterministically extracts top-level filing sections from usable Table of Contents links. It is a standalone Python 3.12+ package in the Finbot workspace.

The package intentionally keeps raw acquisition separate from derived section output. Section parsing uses internal TOC anchors only. Heading-based fallback, semantic summarization, XBRL processing, feature generation, LLMs, databases, AWS, and S3 remain out of scope.

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
SEC_USER_AGENT=Finbot your-email@example.com
SEC_CIK_OVERRIDES=EXAMPLE:123456
```

Replace the download folder, section folder, and User-Agent dummy values with real values. Remove the optional example CIK override unless you need a ticker-continuity override. The package reads `SEC_USER_AGENT` from `.env` automatically. A process environment variable with the same name takes precedence. The package has no fabricated fallback identity and exits with a configuration error if this setting is absent. Requests use a 30-second timeout and are limited to at most 10 starts per second by default.

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
SEC_USER_AGENT=Finbot your-real-email@example.com
SEC_CIK_OVERRIDES=XOM:34088
```

Process environment variables take precedence over the config file. `--download-folder PATH` can override the configured download path for one command. Existing `filing.html` files are skipped by default. `--overwrite` explicitly replaces both the original document and its metadata. `.env` is ignored by Git; `.env.example` contains only dummy values and is safe to commit.

Files use this predictable layout:

```text
/Users/ilan/workspace/finbot/data/raw/filings/
└── AAPL/
    └── 0000320193-25-000079/
        ├── filing.html
        └── metadata.json
```

`filing.html` contains the exact response bytes returned by the primary-document URL. `metadata.json` records the company, CIK, form, filing and report dates, accession number, SEC index and document URLs, primary filename, and UTC download time.

### Batch downloads

For a list of companies, create a text file with one ticker per line. Blank lines and comments beginning with `#` are ignored:

```text
# Large-cap examples
AAPL
MSFT
JPM
WMT
```

Run the package script from any directory:

```bash
repos/finbot-filings/scripts/download_tickers.sh tickers.txt 10-K 3
```

The positional arguments are the ticker file, exact form, and filings per ticker. Form defaults to `10-K` and count defaults to `3`. The script continues when an individual ticker fails, prints a final success/failure summary, and exits nonzero if any ticker failed.

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
Run with `--overwrite` once after upgrading from the earlier canonical-only output
layout so each filing receives a schema-version 2 native-section manifest.

Oversized sections can be split into deterministic, overlapping chunks without
crossing a source-section boundary:

```bash
.venv/bin/finbot-filings parse-sections \
  --form 10-K \
  --max-chunk-chars 24000 \
  --chunk-overlap-chars 1000 \
  --overwrite
```

Sections at or below the maximum remain available as whole section files and do
not receive redundant chunk files.

### Parsing method

The `native-toc-v1` parser:

1. Locates the filing's primary table-based outline using forward internal links. Ordinary Item and note cross-references elsewhere in the filing are excluded.
2. Preserves each outline entry's document-native title, anchor, and physical order. Item labels such as `Item 1 C.` are normalized only for stable IDs and optional mappings.
3. Supports both conventional SEC Item outlines and topic-oriented outlines such as Intel's. Multiple native sections in one row or nonstandard Items such as AAL Items 8A and 8B remain separate.
4. Requires each accepted link to resolve to an exact, forward HTML `id` target.
5. Resolves conflicting Item links using destination and link-text evidence, without treating page-number links as independent section definitions.
6. Sorts resolved targets by physical DOM position and extracts normalized visible text from each target to the next physical target.
7. Adds a canonical SEC section ID only when the Item/Part mapping is deterministic. Otherwise it may add conservative semantic categories while retaining the native section unchanged.

If standard TOC discovery finds no region, a 10-K-only fallback can recognize rows that omit the word `Item`, such as `1 Business`. The fallback activates only when one table contains at least eight distinct canonical sections including Items 1, 1A, 7, and 8; every accepted row must begin with a valid Item token, have a compatible canonical caption, end with a numeric page reference, and link forward to a destination beginning with the exact expected Item heading. Successful use is recorded as `itemless_toc_fallback_used` in diagnostics.

The parser does not inspect headings as a fallback and does not invoke an LLM.
Canonical mapping completeness is not a condition of successful source extraction.

Materially complex filing layouts and possible deterministic or LLM-assisted recovery paths are tracked in [Complex Filing Layouts and Recovery Registry](docs/parser-limitations.md).

### Extraction and mapping status

Source extraction and canonical mapping are reported independently:

- `success` means the primary outline was extracted with no unresolved accepted anchors.
- `partial` means usable native sections were written but one or more accepted anchors could not be resolved.
- `failure` means the parser could not establish a credible source outline.
- Canonical mapping is separately `complete`, `partial`, or `none` and never changes a successful source extraction into a failure.

At least six native sections are required for a 10-Q and eight for a 10-K. These
minimums reject incidental link clusters; they do not require a fixed canonical
section inventory.

Stable failure codes include `unknown_form_type`, `no_toc_found`, `no_recognized_item_links`, `anchor_target_missing`, `ambiguous_part_assignment`, `ambiguous_item_links`, `insufficient_section_anchors`, and `parse_error`.

Example output:

```text
[PASS] AAPL/0000320193-26-000020/filing.html — 11 native sections; 11 exact mappings; 0 semantic-only
[PASS] INTC/0000050863-26-000011/filing.html — 24 native sections; 0 exact mappings; 7 semantic-only
[FAIL] EXAMPLE/0000000000-26-000001/filing.html — no_toc_found

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
Exact mappings:        11
Semantic-only:         7
Unmapped sections:     17
Oversized chunks:      0

Failure reasons:
  no_toc_found: 1
```

### Section output

Each filing receives an isolated derived-data directory:

```text
data/filings/sections/
└── AAPL/
    └── 0000320193-26-000020/
        ├── sections/
        │   ├── 01_part1_item1.txt
        │   ├── 02_part1_item2.txt
        │   └── ...
        ├── chunks/
        │   └── part1_item1/
        │       ├── 001.txt
        │       └── 002.txt
        └── manifest.json
```

Schema-version 2 manifests contain source provenance, parser version, extraction
status, native titles and IDs, anchors, physical ordering, optional canonical IDs,
semantic categories, explicit registrant-caption evidence, section files, and chunk
offsets. Chunk offsets always refer to one source section; chunks never combine
sections. Failed filings receive a diagnostic manifest but no fabricated text.

Downstream feature code should read the manifest, select relevant native sections
or chunks, and record exactly which inputs were sent to a model. Registrant roles,
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
- Section parsing requires a distinguishable TOC region with usable internal anchors and fails instead of falling back to headings.
- Some filings contain ambiguous duplicate links, backward-only cross-links, missing anchor targets, or no link-based TOC and will fail deliberately.
- Noncontiguous canonical Item reconstruction is not attempted; native topic sections remain available instead. Complex cases are documented in [the parser limitations registry](docs/parser-limitations.md).
- Section text is normalized for readability but tables are flattened to text; semantic table reconstruction is not attempted.
- Feature extraction, LLM calls, and semantic summarization are intentionally out of scope.
