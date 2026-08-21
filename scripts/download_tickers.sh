#!/usr/bin/env bash

set -uo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/download_tickers.sh TICKER_FILE [FORM] [COUNT]

Download filings for one ticker per line.

Arguments:
  TICKER_FILE  Text file containing tickers; blank lines and # comments are ignored.
  FORM         Exact SEC form: 10-K or 10-Q (default: 10-K).
  COUNT        Filings per ticker (default: 3).

Environment:
  FINBOT_FILINGS_BIN  Optional path to the finbot-filings executable.

Examples:
  scripts/download_tickers.sh tickers.txt
  scripts/download_tickers.sh tickers.txt 10-Q 5
EOF
}

if [[ ${1:-} == "-h" || ${1:-} == "--help" ]]; then
  usage
  exit 0
fi

if [[ $# -lt 1 || $# -gt 3 ]]; then
  usage >&2
  exit 2
fi

ticker_file=$1
form=${2:-10-K}
count=${3:-3}

case "$form" in
  10-K|10-Q) ;;
  *)
    echo "Error: FORM must be 10-K or 10-Q; received '$form'." >&2
    exit 2
    ;;
esac

if [[ ! "$count" =~ ^[1-9][0-9]*$ ]]; then
  echo "Error: COUNT must be a positive integer; received '$count'." >&2
  exit 2
fi

if [[ ! -f "$ticker_file" ]]; then
  echo "Error: ticker file not found: $ticker_file" >&2
  exit 2
fi

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
package_root=$(cd "$script_dir/.." && pwd)
finbot_filings_bin=${FINBOT_FILINGS_BIN:-"$package_root/.venv/bin/finbot-filings"}
export FINBOT_FILINGS_CONFIG=${FINBOT_FILINGS_CONFIG:-"$package_root/.env"}

if [[ ! -x "$finbot_filings_bin" ]]; then
  echo "Error: finbot-filings executable not found: $finbot_filings_bin" >&2
  echo "Install the package first with: .venv/bin/pip install -e '.[dev]'" >&2
  exit 2
fi

processed=0
succeeded=0
failed=0

while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
  ticker=${raw_line%%#*}
  ticker=${ticker#"${ticker%%[![:space:]]*}"}
  ticker=${ticker%"${ticker##*[![:space:]]}"}
  [[ -z "$ticker" ]] && continue

  processed=$((processed + 1))
  echo
  echo "==> $ticker $form (up to $count filings)"
  if "$finbot_filings_bin" download "$ticker" --form "$form" --count "$count"; then
    succeeded=$((succeeded + 1))
  else
    failed=$((failed + 1))
    echo "Failed: $ticker $form" >&2
  fi
done < "$ticker_file"

echo
echo "Batch complete: $processed processed, $succeeded succeeded, $failed failed."

if [[ $failed -gt 0 ]]; then
  exit 1
fi
