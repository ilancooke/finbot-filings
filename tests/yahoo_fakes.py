"""Synthetic Yahoo wire fixtures, independent of the application's query builder."""

from copy import deepcopy
from datetime import datetime, time, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

TEMPLATE = json.loads((Path(__file__).parent / "fixtures/yahoo_page.json").read_text())


def row(day, *, symbol="AAPL", title="Q3 2026 Earnings Announcement", timing="AMC", at=None):
    return [symbol, "Synthetic company", title, at or day.isoformat() + "T20:00:00Z", timing]


def raw_page(day, size, offset, rows=(), total=None):
    payload = deepcopy(TEMPLATE)
    result = payload["finance"]["result"][0]
    body = json.loads(result["rawCriteria"])
    body["size"], body["offset"] = size, offset
    following = day + timedelta(days=1)
    body["query"]["operands"][2]["operands"][1] = day.isoformat()
    body["query"]["operands"][3]["operands"][1] = following.isoformat()
    result["rawCriteria"] = json.dumps(body)
    result["total"] = len(rows) if total is None else total
    meta = result["criteriaMeta"]
    meta["size"], meta["offset"] = size, offset
    for criterion, date in zip(meta["criteria"][2:], (day, following)):
        criterion["values"] = [datetime.combine(date, time.min, ZoneInfo("America/New_York")).isoformat()]
    result["documents"][0]["rows"] = deepcopy(list(rows))
    return payload


class PageSource:
    def __init__(self, days=None, mutate=None):
        self.days, self.mutate = days or {}, mutate
        self.calls = []
        self.closed = False

    def page(self, day, size, offset, budget):
        budget.remaining()
        budget.attempts += 1
        budget.pages += 1
        self.calls.append((day, size, offset))
        rows = self.days.get(day, [])
        payload = raw_page(day, size, offset, rows[offset:offset + size], len(rows))
        if self.mutate:
            self.mutate(payload, day, size, offset, len(self.calls))
        return payload

    def close(self):
        self.closed = True
