"""Calendar-table sync metadata, outside ISO-date partitions and CIK identities."""

from datetime import date, datetime

from finbot_ingestion.calendar.contracts import CalendarSyncRun, CalendarSyncState, CalendarSyncSuccess
from ..errors import RepositoryDataError
from .serialization import integer, timestamp

SYNC_PARTITION = "__calendar_sync__"


def run_record(run):
    return dict(request_id=run.request_id, provider=run.provider, kind=run.kind,
                start_date=run.start_date.isoformat(), end_date=run.end_date.isoformat(),
                company_ciks=list(run.company_ciks), observed_at=timestamp(run.observed_at))


def read_time(value):
    parsed = datetime.fromisoformat(value)
    if timestamp(parsed) != value:
        raise ValueError("sync timestamps require fixed-width UTC encoding")
    return parsed


def read_run(item):
    if not isinstance(item["company_ciks"], list):
        raise ValueError("invalid company scope")
    return CalendarSyncRun(request_id=item["request_id"], provider=item["provider"], kind=item["kind"],
        start_date=date.fromisoformat(item["start_date"]), end_date=date.fromisoformat(item["end_date"]),
        company_ciks=tuple(item["company_ciks"]), observed_at=read_time(item["observed_at"]))


def state_record(state):
    item = dict(repository_schema_version=1, calendar_record_type="sync", expected_date=SYNC_PARTITION,
                cik=state.provider, revision=state.revision, latest_run=run_record(state.latest_run))
    for kind in ("full", "near_term"):
        success = getattr(state, kind + "_success")
        if success is not None:
            item[kind + "_success"] = dict(run=run_record(success.run),
                completed_at=timestamp(success.completed_at), event_count=success.event_count,
                cancelled_count=success.cancelled_count)
    if state.failure_run is not None:
        item.update(failure_run=run_record(state.failure_run), last_error=state.last_error,
                    last_error_at=timestamp(state.last_error_at))
    return item


def read_state(item):
    try:
        if (integer(item["repository_schema_version"], "schema version") != 1
                or item["calendar_record_type"] != "sync" or item["expected_date"] != SYNC_PARTITION):
            raise ValueError("invalid sync record type/schema")
        successes = {}
        for kind in ("full", "near_term"):
            if kind + "_success" in item:
                value = item[kind + "_success"]
                successes[kind + "_success"] = CalendarSyncSuccess(run=read_run(value["run"]),
                    completed_at=read_time(value["completed_at"]),
                    event_count=integer(value["event_count"], "event_count"),
                    cancelled_count=integer(value["cancelled_count"], "cancelled_count"))
        failures = {}
        if any(name in item for name in ("failure_run", "last_error", "last_error_at")):
            failures = dict(failure_run=read_run(item["failure_run"]), last_error=item["last_error"],
                            last_error_at=read_time(item["last_error_at"]))
        return CalendarSyncState(provider=item["cik"], revision=integer(item["revision"], "revision"),
                                 latest_run=read_run(item["latest_run"]), **successes, **failures)
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        raise RepositoryDataError("invalid calendar sync state") from exc
