"""The operator check stays provider-only and reports collection limits honestly."""

import asyncio
from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path

import pytest

from finbot_ingestion.calendar import check
from finbot_ingestion.calendar.contracts import CalendarSnapshot
from finbot_ingestion.calendar.provider import CalendarDataError, IncompleteCalendarSnapshot
from finbot_ingestion.domain import ExpectedEarningsEvent
from tests.yahoo_fakes import PageSource, row


ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / "infra/seed/companies.prod.json"
START, END = date(2026, 10, 9), date(2026, 11, 7)
NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)


def snapshot(companies):
    return CalendarSnapshot(provider="yahoo", start_date=START, end_date=END,
        company_ciks=tuple(company.cik for company in companies), complete=True,
        events=(ExpectedEarningsEvent(company_cik="320193", ticker="AAPL", expected_date=START,
            synced_at=NOW, provider="yahoo", time_of_day="after_market",
            raw_provider_payload={"private": "must not appear in report"}),))


def test_all_50_company_inputs_are_loaded_without_client_construction():
    companies = check.load_companies(SEED)
    assert len(companies) == len({company.cik for company in companies}) == 50


@pytest.mark.parametrize("changes", [
    {"complete": False}, {"end_date": START}, {"company_ciks": ("320193",)},
    {"provider": "other"},
])
def test_incomplete_or_wrong_scope_never_reports_success(changes):
    companies = check.load_companies(SEED)
    with pytest.raises(IncompleteCalendarSnapshot):
        check.snapshot_report(replace(snapshot(companies), **changes), companies, START, END)


def test_missing_observations_are_visible_and_raw_payload_is_excluded():
    companies = check.load_companies(SEED)
    result = check.snapshot_report(snapshot(companies), companies, START, END)
    assert result["collection_complete"] is True
    assert result["matched_companies"] == 1
    assert len(result["companies_without_observations"]) == 49
    assert "AAPL" not in result["companies_without_observations"]
    assert "private" not in json.dumps(result)
    with pytest.raises(CalendarDataError):
        check.snapshot_report(replace(snapshot(companies), events=snapshot(companies).events * 2),
                              companies, START, END)


def test_operator_command_uses_real_bounded_adapter_and_closes_it(tmp_path, monkeypatch, capsys):
    def forbidden_client(*args, **kwargs):
        raise AssertionError("the calendar check must not construct an AWS client")

    monkeypatch.setattr("boto3.Session.client", forbidden_client)
    monkeypatch.setattr("boto3.Session.resource", forbidden_client)
    source = PageSource({START: [row(START)]})
    real = check.YahooEarningsCalendarProvider
    monkeypatch.setattr(check, "YahooEarningsCalendarProvider",
        lambda config, execution: real(config, execution, client=source))
    path = tmp_path / "report.json"
    assert check.main(["--companies-file", str(SEED), "--start-date", str(START),
                       "--output", str(path)]) == 0
    report = json.loads(path.read_text())
    assert (report["start_date"], report["end_date"], report["days_requested"]) == (str(START), str(END), 30)
    assert report["requested_companies"] == 50
    assert report["status"] == "collection_complete" and report["matched_companies"] == 1
    assert report["aws_writes"] is False
    assert {day for day, _, _ in source.calls} == {START + check.timedelta(days=i) for i in range(30)}
    assert source.closed
    assert "events" not in json.loads(capsys.readouterr().out)


def test_operator_command_sanitizes_failure_and_closes_provider(tmp_path, monkeypatch, capsys):
    class FailedProvider:
        stopped = closed = False

        def __init__(self, *args):
            pass

        async def fetch_events(self, *args):
            raise RuntimeError("cookie=secret https://example.invalid/?crumb=secret")

        def stop_admissions(self):
            self.stopped = True

        async def close(self):
            self.closed = True

    provider = FailedProvider()
    monkeypatch.setattr(check, "YahooEarningsCalendarProvider", lambda *args: provider)
    path = tmp_path / "report.json"
    assert check.main(["--companies-file", str(SEED), "--output", str(path)]) == 1
    report = json.loads(path.read_text())
    assert report["status"] == "failed" and report["error_type"] == "RuntimeError"
    assert report["collection_complete"] is False
    assert provider.stopped and provider.closed
    assert "secret" not in path.read_text() + capsys.readouterr().out


def test_existing_report_is_preserved_before_any_provider_call(tmp_path, monkeypatch):
    async def forbidden(*args):
        raise AssertionError("must not contact the provider")

    monkeypatch.setattr(check, "fetch", forbidden)
    path = tmp_path / "report.json"
    path.write_text("retain previous evidence")
    assert check.main(["--companies-file", str(SEED), "--output", str(path)]) == 1
    assert path.read_text() == "retain previous evidence"


def test_cancelled_check_stops_and_closes_owned_provider(monkeypatch):
    class SlowProvider:
        def __init__(self, *args):
            self.entered = asyncio.Event()
            self.stop = asyncio.Event()
            self.closed = False

        async def fetch_events(self, *args):
            self.entered.set()
            await self.stop.wait()

        def stop_admissions(self):
            self.stop.set()

        async def close(self):
            self.closed = True

    async def scenario():
        provider = SlowProvider()
        monkeypatch.setattr(check, "YahooEarningsCalendarProvider", lambda *args: provider)
        task = asyncio.create_task(check.fetch((), START, END, None))
        await provider.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.stop.is_set() and provider.closed

    asyncio.run(scenario())
