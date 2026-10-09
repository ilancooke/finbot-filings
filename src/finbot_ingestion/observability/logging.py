"""Allowlisted structured fields; never print arbitrary provider/SDK payloads."""

from datetime import datetime, timezone
import json
import logging
import sys

FIELDS = frozenset(("operation", "cik", "ticker", "accession_number", "artifact_id", "work_id",
    "attempt_number", "http_status", "error_type", "will_retry", "provider", "sync_kind", "event_count",
    "cancelled_count", "company_count", "start_date", "end_date", "change", "expected_date",
    "time_of_day", "previous_date", "previous_time_of_day", "reason", "policy_version", "task", "failure_id"))


class JSONFormatter(logging.Formatter):
    def format(self, record):
        # Exception messages may contain provider URLs/credentials; types suffice.
        result = {"timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                  "service": "finbot-ingestion", "level": record.levelname, "logger": record.name,
                  "message": record.getMessage() if record.name.startswith("finbot_ingestion") else "External library log"}
        for name in FIELDS:
            value = getattr(record, name, None)
            if value is not None and isinstance(value, (str, int, float, bool)):
                result[name] = value[:2048] if isinstance(value, str) else value
        if record.exc_info:
            result["error_type"] = record.exc_info[0].__name__
        return json.dumps(result, allow_nan=False)


def configure_logging(level="INFO"):
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JSONFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
