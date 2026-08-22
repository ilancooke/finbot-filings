"""Human-readable inspection and export of normalized XBRL facts."""

from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

import pyarrow.parquet as pq

from finbot_filings.layout import form_directory

SUPPORTED_OUTPUT_FORMATS = frozenset({"table", "json", "csv"})


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _fact_paths(
    xbrl_root: Path,
    *,
    ticker: str,
    form_type: str | None,
    accession_number: str | None,
) -> list[Path]:
    ticker_directory = xbrl_root / ticker.strip().upper()
    form_pattern = form_directory(form_type) if form_type else "*"
    accession_pattern = accession_number.strip() if accession_number else "*"
    return sorted(ticker_directory.glob(f"{form_pattern}/{accession_pattern}/facts.parquet"))


def _matches_concept(fact: dict[str, Any], concept: str | None) -> bool:
    if not concept:
        return True
    wanted = concept.strip().lower()
    name = str(fact.get("concept_name", "")).lower()
    prefix = str(fact.get("concept_prefix", "")).lower()
    qname = str(fact.get("concept_qname", "")).lower()
    return wanted in {name, f"{prefix}:{name}"} or qname.endswith("}" + wanted)


def _table(rows: list[dict[str, Any]]) -> str:
    headings = ("concept", "value", "unit", "period", "dimensions")
    display_rows: list[tuple[str, str, str, str, str]] = []
    for row in rows:
        period = row.get("period_instant") or (
            f"{row.get('period_start') or ''}..{row.get('period_end') or ''}"
        )
        value = str(row.get("value_text", "")).replace("\n", " ")
        if len(value) > 60:
            value = value[:57] + "..."
        dimensions = str(row.get("dimensions_json", "[]"))
        if len(dimensions) > 40:
            dimensions = dimensions[:37] + "..."
        display_rows.append(
            (
                f"{row.get('concept_prefix') or ''}:{row['concept_name']}".lstrip(":"),
                value,
                str(row.get("unit_display") or ""),
                str(period),
                dimensions,
            )
        )
    widths = [
        max(len(headings[index]), *(len(row[index]) for row in display_rows))
        for index in range(len(headings))
    ]
    lines = ["  ".join(value.ljust(widths[index]) for index, value in enumerate(headings))]
    lines.append("  ".join("-" * width for width in widths))
    lines.extend(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in display_rows
    )
    return "\n".join(lines)


def show_xbrl_facts(
    *,
    xbrl_root: Path,
    ticker: str,
    form_type: str | None = None,
    accession_number: str | None = None,
    concept: str | None = None,
    output_format: str = "table",
    limit: int = 50,
    output_path: Path | None = None,
    printer: Callable[[str], None] = print,
) -> int:
    """Read Parquet facts and render a bounded diagnostic view or export."""
    if output_format not in SUPPORTED_OUTPUT_FORMATS:
        raise ValueError("output format must be table, json, or csv")
    if limit <= 0:
        raise ValueError("limit must be greater than zero")
    paths = _fact_paths(
        xbrl_root,
        ticker=ticker,
        form_type=form_type,
        accession_number=accession_number,
    )
    rows: list[dict[str, Any]] = []
    for path in paths:
        for row in pq.read_table(path).to_pylist():
            if _matches_concept(row, concept):
                rows.append(row)
                if len(rows) >= limit:
                    break
        if len(rows) >= limit:
            break

    if output_format == "json":
        rendered = json.dumps(rows, indent=2, default=_json_default) + "\n"
    elif output_format == "csv":
        destination = io.StringIO()
        if rows:
            writer = csv.DictWriter(destination, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        rendered = destination.getvalue()
    else:
        rendered = _table(rows) + "\n" if rows else "No matching facts.\n"

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    else:
        printer(rendered.rstrip("\n"))
    return len(rows)
