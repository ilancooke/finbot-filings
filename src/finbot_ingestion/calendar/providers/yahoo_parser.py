"""Private raw-page validation. No DataFrames or Yahoo structures escape the adapter."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
import json
import re
from zoneinfo import ZoneInfo

from finbot_ingestion.domain.identity import normalize_ticker
from finbot_ingestion.domain.validation import utc_datetime
from ..provider import IncompleteCalendarSnapshot

FIELDS = ("ticker", "companyshortname", "eventname", "startdatetime", "startdatetimetype")
REQUIRED_TYPES = {"ticker": "STRING", "eventname": "STRING", "startdatetime": "DATE",
                  "startdatetimetype": "STRING"}


def query_body(day, size, offset):
    return {"sortType": "DESC", "entityIdType": "sp_earnings", "sortField": "intradaymarketcap",
            "includeFields": list(FIELDS), "size": size, "offset": offset,
            "query": {"operator": "AND", "operands": [
                {"operator": "EQ", "operands": ["region", "us"]},
                {"operator": "OR", "operands": [
                    {"operator": "EQ", "operands": ["eventtype", "EAD"]},
                    {"operator": "EQ", "operands": ["eventtype", "ERA"]}]},
                {"operator": "GTE", "operands": ["startdatetime", day.isoformat()]},
                {"operator": "LTE", "operands": ["startdatetime", (day + timedelta(days=1)).isoformat()]}]}}


def safe_text(value, *, maximum, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("invalid scheduling text")
    return value


@dataclass(frozen=True, slots=True)
class YahooObservation:
    ticker: str
    title: str | None
    at: datetime
    timing: str | None

    @property
    def key(self):
        return self.ticker, self.title, self.at

    @property
    def replacement_hint(self):
        match = re.fullmatch(r"Q([1-4]) ([12]\d{3}) Earnings Announcement", self.title or "")
        return None if match is None else f"quarterly-announcement-v1/{match[2]}/Q{match[1]}"


@dataclass(frozen=True, slots=True)
class YahooPage:
    total: int
    schema: tuple[tuple[str, str], ...]
    observations: tuple[YahooObservation, ...]


def parse_page(payload, *, day, size, offset, zone):
    try:
        finance = payload["finance"]
        if finance.get("error") is not None:
            raise ValueError("provider error")
        results = finance["result"]
        if not isinstance(results, list) or len(results) != 1:
            raise ValueError("one calendar result required")
        result = results[0]
        total = result["total"]
        if type(total) is not int or total < 0:
            raise ValueError("invalid total")
        # Echoed original request proves the filters and offsets; criteriaMeta proves
        # how Yahoo interpreted date-only bounds (including DST).
        raw = json.loads(result["rawCriteria"])
        if raw != query_body(day, size, offset):
            raise ValueError("raw query scope mismatch")
        meta = result["criteriaMeta"]
        if (meta["size"] != size or meta["offset"] != offset
                or meta["entityIdType"] != "SP_EARNINGS" or meta["topOperator"] != "AND"
                or meta["sortField"] != "intradaymarketcap" or meta["sortType"] != "DESC"):
            raise ValueError("query metadata mismatch")
        criteria = meta["criteria"]
        if len(criteria) != 4:
            raise ValueError("unexpected filter")
        region, kind, lower, upper = criteria
        if (region["field"] != "region" or region["operators"] != ["EQ"]
                or region.get("values", []) not in ([], ["us"]) or region.get("labelsSelected", []) not in ([], [53])
                or kind["field"] != "eventtype" or kind["operators"] != ["EQ", "EQ"]
                or kind["values"] != ["EAD", "ERA"]):
            raise ValueError("unexpected source scope")
        for bound, operator, date in ((lower, "GTE", day), (upper, "LTE", day + timedelta(days=1))):
            if bound["field"] != "startdatetime" or bound["operators"] != [operator] or len(bound["values"]) != 1:
                raise ValueError("missing date evidence")
            parsed = datetime.fromisoformat(bound["values"][0])
            expected = datetime.combine(date, time.min, ZoneInfo(zone))
            if parsed.tzinfo is None or parsed != expected:
                raise ValueError("source interpreted date scope incorrectly")
        documents = result["documents"]
        if not isinstance(documents, list) or len(documents) != 1 or documents[0]["entityIdType"] != "SP_EARNINGS":
            raise ValueError("missing calendar document/schema")
        columns, rows = documents[0]["columns"], documents[0]["rows"]
        if not isinstance(columns, list) or not isinstance(rows, list):
            raise ValueError("invalid rows/schema")
        schema = tuple((column["id"], column["type"]) for column in columns)
        names = [item[0] for item in schema]
        if len(names) != len(set(names)) or any(dict(schema).get(name) != type_ for name, type_ in REQUIRED_TYPES.items()):
            raise ValueError("missing/conflicting columns")
        if len(rows) > size:
            raise ValueError("overfull page")
        observations = []
        start = datetime.combine(day, time.min, ZoneInfo(zone))
        end = datetime.combine(day + timedelta(days=1), time.min, ZoneInfo(zone))
        for row in rows:
            if not isinstance(row, list) or len(row) != len(names):
                raise ValueError("malformed row")
            values = dict(zip(names, row))
            symbol = normalize_ticker(safe_text(values["ticker"], maximum=64))
            title = safe_text(values["eventname"], maximum=256, nullable=True)
            timing = safe_text(values["startdatetimetype"], maximum=32, nullable=True)
            at = utc_datetime(datetime.fromisoformat(values["startdatetime"]), "source event time")
            if not start <= at <= end:
                raise ValueError("row outside source slice")
            observations.append(YahooObservation(symbol, title, at, timing))
        return YahooPage(total, schema, tuple(observations))
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError) as exc:
        raise IncompleteCalendarSnapshot("invalid Yahoo page or scope evidence") from None
