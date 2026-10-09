"""Raw Yahoo completeness, date boundaries and owned HTTP execution are offline."""

import asyncio
from datetime import date, datetime, timedelta, timezone
import threading
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from yahoo_fakes import PageSource, raw_page, row
from finbot_ingestion.calendar.config import CalendarConfig
from finbot_ingestion.calendar.provider import IncompleteCalendarSnapshot
from finbot_ingestion.calendar.providers.yahoo import YahooEarningsCalendarProvider
from finbot_ingestion.calendar.providers.yahoo_client import FetchBudget, YahooSession, YahooStopped
from finbot_ingestion.calendar.providers.yahoo_config import YahooConfig
from finbot_ingestion.config import ConfigurationError
from finbot_ingestion.domain import Company
from finbot_ingestion.execution import BlockingExecution

DAY = date(2026, 10, 13)
COMPANY = Company("AAPL", "320193", "Synthetic company")


@pytest.fixture
def no_backoff(monkeypatch):
    def wait(budget, seconds):
        if seconds >= budget.remaining():
            raise IncompleteCalendarSnapshot("deadline")
    monkeypatch.setattr(FetchBudget, "wait", wait)


def fetch(source, *, start=DAY, end=DAY, config=None, companies=(COMPANY,)):
    async def scenario():
        with BlockingExecution(name="yahoo-test") as execution:
            provider = YahooEarningsCalendarProvider(config or YahooConfig(), execution, client=source)
            try:
                return await provider.fetch_events(start, end, companies)
            finally:
                await provider.close()
    return asyncio.run(scenario())


@pytest.mark.parametrize("changes", [{"page_size":101}, {"request_attempts":6}, {"http_timeout_seconds":0},
    {"min_request_interval_seconds":float("nan")}, {"max_fetch_seconds":float("inf")},
    {"max_pages_per_slice":True}, {"max_raw_rows":0}, {"cache_dir":"relative"},
    {"http_timeout_seconds":601}])
def test_invalid_yahoo_config(changes):
    with pytest.raises(ConfigurationError):
        YahooConfig(**changes)


def test_yahoo_settings_do_not_create_files_or_clients(tmp_path):
    path = tmp_path / "not-created"
    config = YahooConfig.from_env({"YAHOO_CACHE_DIR":str(path), "YAHOO_PAGE_SIZE":"25"})
    assert config.page_size == 25 and not path.exists()
    calendar = CalendarConfig.from_env({"CALENDAR_PROVIDER":"yahoo"})
    assert (calendar.lookahead_days, calendar.provider_attempts, calendar.near_term_refresh_seconds) == (30, 1, 0)
    assert CalendarConfig.from_env({}).provider_attempts == 3
    with pytest.raises(ConfigurationError):
        YahooConfig.from_env({"YAHOO_PAGE_SIZE":"oops"})


def test_full_30_day_scope_requires_every_slice_and_verification(no_backoff):
    final = DAY + timedelta(days=29)
    source = PageSource({DAY:[row(DAY)], final:[row(final, title="Q4 2026 Earnings Announcement")]})
    result = fetch(source, end=final)
    assert result.complete and result.company_ciks == (COMPANY.cik,)
    assert {e.expected_date for e in result.events} == {DAY, final}
    assert len(source.calls) == 64
    assert {call[0] for call in source.calls} == {DAY + timedelta(days=n) for n in range(30)}
    assert source.closed
    assert all(e.provider_event_id is None and e.provider_updated_at is None for e in result.events)


@pytest.mark.parametrize("day", [date(2026,3,8), date(2026,11,1)])
def test_dst_bounds_and_adjacent_midnight_overlap(day, no_backoff):
    next_day = day + timedelta(days=1)
    local = datetime.combine(next_day, datetime.min.time(), ZoneInfo("America/New_York"))
    observation = row(next_day, at=local.astimezone(timezone.utc).isoformat())
    source = PageSource({day:[observation], next_day:[observation]})
    result = fetch(source, start=day, end=next_day)
    assert len(result.events) == 1 and result.events[0].expected_date == next_day
    assert fetch(PageSource({day:[observation]}), start=day, end=day).events == ()


@pytest.mark.parametrize("count", [0,1,2,3,4])
def test_multipage_exact_and_short_terminal_evidence(count, no_backoff):
    source = PageSource({DAY:[row(DAY, symbol=f"OTHER{n}") for n in range(count)]})
    result = fetch(source, config=YahooConfig(page_size=2))
    assert result.complete and result.events == ()
    expected_offsets = list(range(0, count, 2)) + [count]
    assert [c[2] for c in source.calls] == expected_offsets * 2


@pytest.mark.parametrize("fault", ["missing_schema", "bad_type", "bad_criteria", "wrong_region", "wrong_offset",
    "wrong_bounds", "nonfinite_time", "naive_time", "overfull", "short_page", "early_empty", "duplicate",
    "changing_total", "changing_schema", "changed_verification", "repeated_page", "provider_error"])
def test_invalid_or_unstable_collection_is_incomplete(fault, no_backoff):
    rows = [row(DAY, symbol=f"OTHER{n}") for n in range(3)]
    if fault == "duplicate":
        rows[1] = rows[0]
    def mutate(payload, day, size, offset, call):
        result = payload["finance"]["result"][0]
        document = result["documents"][0]
        if fault == "missing_schema": document["columns"] = []
        if fault == "bad_type": document["columns"][3]["type"] = "STRING"
        if fault == "bad_criteria": result["rawCriteria"] = "{}"
        if fault == "wrong_region": result["criteriaMeta"]["criteria"][0]["labelsSelected"] = [99]
        if fault == "wrong_offset": result["criteriaMeta"]["offset"] = offset + 1
        if fault == "wrong_bounds": result["criteriaMeta"]["criteria"][3]["values"] = [day.isoformat()+"T00:00:00-04:00"]
        if fault == "nonfinite_time" and document["rows"]: document["rows"][0][3] = float("nan")
        if fault == "naive_time" and document["rows"]: document["rows"][0][3] = day.isoformat()+"T20:00:00"
        if fault == "overfull": document["rows"] = rows
        if fault == "short_page" and offset == 0: document["rows"] = rows[:1]
        if fault == "early_empty" and offset == 0: document["rows"] = []
        if fault == "changing_total" and offset: result["total"] += 1
        if fault == "changing_schema" and offset: document["columns"][1]["type"] = "OTHER"
        if fault == "changed_verification" and call % 6 == 4: document["rows"][0][2] = "Changed announcement"
        if fault == "repeated_page" and offset: document["rows"] = rows[:2]
        if fault == "provider_error": payload["finance"]["error"] = {"description":"secret"}
    with pytest.raises(IncompleteCalendarSnapshot):
        fetch(PageSource({DAY:rows}, mutate), config=YahooConfig(page_size=2))


@pytest.mark.parametrize("config", [YahooConfig(max_pages_per_slice=1), YahooConfig(max_raw_rows=1)])
def test_source_limits_cannot_claim_complete(config, no_backoff):
    with pytest.raises(IncompleteCalendarSnapshot):
        fetch(PageSource({DAY:[row(DAY)]}), config=config)


@pytest.mark.parametrize("timing,expected", [("BMO","before_market"), ("AMC","after_market"),
    ("TNS","unknown"), ("TAS","unknown"), (None,"unknown"), ("NEW_CODE","unknown")])
def test_coarse_timing_and_quarterly_hint(timing, expected, no_backoff):
    event = fetch(PageSource({DAY:[row(DAY, timing=timing)]})).events[0]
    assert event.time_of_day == expected
    assert event.replacement_hint == "quarterly-announcement-v1/2026/Q3"
    assert set(event.raw_provider_payload) == {"title", "timing", "source_event_at"}


@pytest.mark.parametrize("title", [None,"Earnings Call","H1 2026 Earnings Announcement","Q3 2026 Earnings Announcement estimated"])
def test_unrecognized_titles_supply_no_replacement_evidence(title, no_backoff):
    assert fetch(PageSource({DAY:[row(DAY, title=title)]})).events[0].replacement_hint is None


def test_same_cik_date_conflict_is_explicit(no_backoff):
    with pytest.raises(IncompleteCalendarSnapshot):
        fetch(PageSource({DAY:[row(DAY), row(DAY, title="Q4 2026 Earnings Announcement")]}))


def response(status=200, *, payload=None, headers=None):
    return SimpleNamespace(status_code=status, content=b"{}", headers=headers or {}, json=lambda: payload)


def test_http_admissions_include_auth_retries_redirects_and_timeouts(monkeypatch, no_backoff):
    config = YahooConfig(max_http_attempts=3)
    stop = threading.Event()
    session = YahooSession(config)
    session.budget = FetchBudget(config, stop)
    calls = []
    def perform(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return response(302, headers={"Location":"https://query1.finance.yahoo.com/next"}) if len(calls) == 1 else response()
    monkeypatch.setattr(session, "_perform", perform)
    try:
        session.request("GET", "https://fc.yahoo.com", timeout=1000, allow_redirects=True)
        session.request("POST", "https://query1.finance.yahoo.com/data")
        with pytest.raises(IncompleteCalendarSnapshot):
            session.request("GET", "https://query1.finance.yahoo.com/data")
        assert session.budget.attempts == 3
        assert all(c[2]["timeout"] <= 20 and c[2]["allow_redirects"] is False for c in calls)
        stop.set()
        with pytest.raises(YahooStopped):
            session.request("GET", "https://fc.yahoo.com")
    finally:
        session.close()


def test_deadline_and_stop_during_pacing():
    clock = [0.0]
    stop = threading.Event()
    budget = FetchBudget(YahooConfig(), stop, monotonic=lambda:clock[0])
    clock[0] = 600
    with pytest.raises(IncompleteCalendarSnapshot): budget.remaining()
    budget = FetchBudget(YahooConfig(), stop)
    timer = threading.Timer(.01, stop.set)
    timer.start()
    with pytest.raises(YahooStopped): budget.wait(5)
    timer.join()


def test_real_pinned_client_auth_seam_and_private_cache(monkeypatch, tmp_path, no_backoff):
    from yfinance import cache
    calls = []
    def perform(session, method, url, **kwargs):
        calls.append(url)
        if url.endswith("getcrumb"):
            return SimpleNamespace(status_code=200, content=b"testcrumb", text="testcrumb", headers={})
        if "visualization" in url:
            body = kwargs["json"]
            day = date.fromisoformat(body["query"]["operands"][2]["operands"][1])
            return response(payload=raw_page(day, body["size"], body["offset"]))
        return response(404)
    monkeypatch.setattr(YahooSession, "_perform", perform)
    monkeypatch.setattr(cache._CookieCacheManager, "_Cookie_cache", None)
    monkeypatch.setattr(cache._TzCacheManager, "_tz_cache", None)
    result = fetch(None, config=YahooConfig(cache_dir=str(tmp_path / "private")))
    assert result.complete and not result.events
    assert any(url.endswith("getcrumb") for url in calls)
    assert sum("visualization" in url for url in calls) == 2
    assert (tmp_path / "private").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("status", [403, 429, 503, "transport"])
def test_authentication_failures_have_finite_retries_and_safe_logs(status, monkeypatch, tmp_path, no_backoff, caplog):
    from curl_cffi import requests
    from yfinance import cache
    from finbot_ingestion.calendar.provider import CalendarTransientError
    calls = []
    secret = "synthetic-cookie-crumb-secret"
    def perform(session, method, url, **kwargs):
        calls.append(url)
        if status == "transport":
            raise requests.exceptions.RequestException(secret)
        return SimpleNamespace(status_code=status, content=secret.encode(), text=secret,
            headers={"Retry-After":"999999999999"})
    monkeypatch.setattr(YahooSession, "_perform", perform)
    monkeypatch.setattr(cache._CookieCacheManager, "_Cookie_cache", None)
    monkeypatch.setattr(cache._TzCacheManager, "_tz_cache", None)
    with pytest.raises(CalendarTransientError) as error:
        fetch(None, config=YahooConfig(cache_dir=str(tmp_path / "private")))
    assert len(calls) == 2
    assert secret not in str(error.value) and secret not in caplog.text


@pytest.mark.parametrize("target", ["https://example.com/path", "http://query1.finance.yahoo.com/path",
    "https://user:password@query1.finance.yahoo.com/path", "https://query1.finance.yahoo.com:444/path"])
def test_unsafe_redirects_stop_before_the_next_native_request(target, monkeypatch, no_backoff):
    session = YahooSession(YahooConfig())
    session.budget = FetchBudget(session.config, threading.Event())
    calls = []
    def perform(method, url, **kwargs):
        calls.append(url)
        return response(302, headers={"Location":target})
    monkeypatch.setattr(session, "_perform", perform)
    try:
        with pytest.raises(IncompleteCalendarSnapshot):
            session.request("GET", "https://query1.finance.yahoo.com/path")
        assert len(calls) == 1
    finally:
        session.close()


def test_async_cancellation_keeps_executor_slot_until_http_finishes(no_backoff):
    entered, released = threading.Event(), threading.Event()
    source = PageSource()
    original = source.page
    def page(*args):
        entered.set()
        released.wait(2)
        return original(*args)
    source.page = page
    async def scenario():
        with BlockingExecution(name="yahoo-test") as execution:
            provider = YahooEarningsCalendarProvider(YahooConfig(), execution, client=source)
            task = asyncio.create_task(provider.fetch_events(DAY, DAY, (COMPANY,)))
            while not entered.is_set(): await asyncio.sleep(.001)
            provider.stop_admissions()
            task.cancel()
            await asyncio.sleep(.005)
            task.cancel()
            assert not task.done() and execution._active == 1 and not source.closed
            released.set()
            with pytest.raises(asyncio.CancelledError): await task
            await provider.close()
            assert source.closed and len(source.calls) == 0
    asyncio.run(scenario())
