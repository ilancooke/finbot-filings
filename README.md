# finbot-filings

`finbot-filings` discovers public SEC 10-K and 10-Q filings and stores each filing's original primary HTML document plus reproducibility metadata. It is a standalone Python 3.12+ package in the Finbot workspace.

This first milestone intentionally covers only filing discovery and immutable local download. Parsing, HTML cleanup, section extraction, XBRL, feature generation, LLMs, databases, AWS, and S3 are out of scope.

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
SEC_USER_AGENT=Finbot your-email@example.com
```

Replace both dummy values with real values. The package reads `SEC_USER_AGENT` from `.env` automatically. A process environment variable with the same name takes precedence. The package has no fabricated fallback identity and exits with a configuration error if this setting is absent. Requests use a 30-second timeout and are limited to at most 10 starts per second by default.

## Discovery

```bash
finbot-filings discover AAPL --form 10-K --count 3
finbot-filings discover JPM --form 10-Q --count 3
```

Ticker matching is case-insensitive. Only exact `10-K` and `10-Q` forms are supported, so amendments such as `10-K/A` and `10-Q/A` are excluded. The default count is 5, and count must be positive.

> Filings are ordered by SEC filing date descending. The report period does not determine recency.

Discovery resolves tickers with the SEC-maintained `company_tickers.json` file and reads filing metadata from the company's SEC submissions JSON. It starts with `filings.recent`. When that data contains fewer exact-form matches than requested, it loads the SEC-referenced `filings.files` metadata newest-first until the requested count is satisfied or history is exhausted. Combined records are deduplicated by accession number and ordered globally by filing date.

## Download

```bash
finbot-filings download AAPL --form 10-K --count 3
finbot-filings download AAPL --form 10-K --count 3 --overwrite
```

The package-level `.env` configures the download directory and SEC identity:

```dotenv
DOWNLOAD_FOLDER=/Users/ilan/workspace/finbot/data/raw/filings/
SEC_USER_AGENT=Finbot your-real-email@example.com
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
- Filing parsing and feature extraction are intentionally out of scope.
