"""SEC attempts reconcile across bounded metrics and ordinary ticker-detail logs."""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import json

import pytest
import requests

from finbot_ingestion.domain import Company, Filing
from finbot_ingestion.observability.metrics import Metrics
from finbot_ingestion.sec.errors import SECRequestStopped
from finbot_ingestion.sec.urls import filing_index_url

from tests.support.sec_transport import make, Response, URL


BASE = (("Environment", "prod"), ("Service", "finbot-ingestion"))


def series(output):
    """Extract and sum each series exactly as EMF dimension sets specify."""
    counts = defaultdict(int)
    for line in output:
        event = json.loads(line)
        for directive in event.get("_aws", {}).get("CloudWatchMetrics", []):
            assert directive["Namespace"] == "Finbot/Ingestion"
            for metric in directive["Metrics"]:
                if metric["Name"] == "SecRequests":
                    assert metric["Unit"] == "Count"
                    for dimensions in directive["Dimensions"]:
                        assert "Ticker" not in dimensions
                        key = tuple(sorted((name, event[name]) for name in dimensions))
                        counts[key] += event["SecRequests"]
    return counts


def key(**dimensions):
    return tuple(sorted((*BASE, *dimensions.items())))


def details(output):
    counts = defaultdict(int)
    for line in output:
        event = json.loads(line)
        if event.get("Event") == "sec_request_breakdown":
            assert "_aws" not in event
            assert event["Service"] == "finbot-ingestion"
            assert event["Environment"] == "prod"
            counts[(event["Ticker"], event["CallType"])] += event["SecRequests"]
    return dict(counts)


def timestamp(event):
    return event["_aws"]["Timestamp"] if "_aws" in event else event["Timestamp"]


def test_seven_http_attempts_reconcile_total_call_types_and_ticker_logs():
    output = []
    client, session, _ = make([Response() for _ in range(7)])
    client.metrics = Metrics(sink=output.append, environment="prod", now_ms=lambda: 123)
    for ticker in ("AAPL", "HOVR"):
        for operation in ("submissions", "filing_index", "filing_directory"):
            client._request(URL, operation=operation, context={"ticker": ticker})
    client.download_document(URL, context={"ticker": "AAPL"})
    client.metrics.count("RuntimeHealthy")
    client.metrics.flush()

    counts = series(output)
    assert len(session.calls) == counts[BASE] == 7
    assert len(counts) == 5
    for call_type, expected in (("submissions", 2), ("filing_index", 2),
                                ("filing_directory", 2), ("document", 1)):
        assert counts[key(CallType=call_type)] == expected
    expected_pairs = {(ticker, call_type): 1 for ticker in ("AAPL", "HOVR")
                      for call_type in ("submissions", "filing_index", "filing_directory")}
    expected_pairs[("AAPL", "document")] = 1
    assert details(output) == expected_pairs
    assert sum(v for k, v in counts.items() if len(k) == 3) == 7
    events = [json.loads(line) for line in output]
    assert {timestamp(event) for event in events} == {123}
    assert events[0]["RuntimeHealthy"] == 1
    assert all(directive["Metrics"] == [{"Name": "SecRequests", "Unit": "Count"}]
               for event in events[1:] for directive in event.get("_aws", {}).get("CloudWatchMetrics", []))


def test_network_retry_and_redirect_each_count_under_original_company_and_type():
    output = []
    client, session, _ = make([requests.Timeout("timeout"),
        Response(status=302, headers={"Location": "/Archives/next"}), Response()])
    client.metrics = Metrics(sink=output.append, environment="prod")
    client.download_document(URL, context={"ticker": "AAPL"})
    client.metrics.flush()
    counts = series(output)
    assert len(session.calls) == counts[BASE] == 3
    assert counts[key(CallType="document")] == 3
    assert details(output) == {("AAPL", "document"): 3}
    assert session.calls[-1][1].endswith("/Archives/next")
    assert json.loads(output[0])["RetryAttempts"] == 1


def test_public_poll_and_index_methods_supply_company_context():
    root = Path(__file__).parents[1] / "fixtures" / "sec_package"
    empty = {"cik": 320193, "filings": {"recent": {
        "accessionNumber": [], "form": [], "acceptanceDateTime": []}}}
    client, session, _ = make([Response(json.dumps(empty).encode()),
        Response((root / "index.html").read_bytes()), Response((root / "directory.json").read_bytes())])
    output = []
    client.metrics = Metrics(sink=output.append, environment="prod")
    company = Company("AAPL", "320193", "Apple")
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    accession = "0000320193-26-000001"
    filing = Filing(accession_number=accession, company_cik=company.cik, ticker=company.ticker,
        form_type="8-K", filed_at=now, discovered_at=now,
        filing_index_url=filing_index_url(company.cik, accession))
    assert client.get_company_submissions(company) == []
    assert client.get_filing_index(filing).primary_document_name == "Primary.htm"
    client.metrics.flush()
    counts = series(output)
    assert len(session.calls) == counts[BASE] == 3
    for call_type in ("submissions", "filing_index", "filing_directory"):
        assert counts[key(CallType=call_type)] == 1
    assert details(output) == {("AAPL", call_type): 1
                              for call_type in ("submissions", "filing_index", "filing_directory")}


def test_missing_context_is_counted_as_unknown_but_stopped_admissions_are_not_counted():
    output = []
    client, session, _ = make([Response()])
    client.metrics = Metrics(sink=output.append, environment="prod")
    client.download_document(URL)
    client.stop_admissions()
    with pytest.raises(SECRequestStopped):
        client.download_document(URL, context={"ticker": "AAPL"})
    client.metrics.flush()
    counts = series(output)
    assert len(session.calls) == counts[BASE] == 1
    assert counts[key(CallType="document")] == 1
    assert details(output) == {("UNKNOWN", "document"): 1}


def test_flush_captures_atomic_batch_and_concurrent_updates_go_to_next_batch():
    output = []
    def sink(line):
        output.append(line)
        if len(output) == 1:
            # Simulate dispatches arriving after the snapshot while it is emitted.
            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(lambda _: metrics.count_sec_request("document", "HOVR"), range(400)))
    times = iter((1000, 2000))
    metrics = Metrics(sink=sink, environment="prod", now_ms=lambda: next(times))
    metrics.count_sec_request("submissions", "AAPL")
    metrics.flush()
    first = series(output)
    assert first == {BASE: 1, key(CallType="submissions"): 1}
    assert details(output) == {("AAPL", "submissions"): 1}
    offset = len(output)
    metrics.flush()
    second = series(output[offset:])
    assert second == {BASE: 400, key(CallType="document"): 400}
    assert details(output[offset:]) == {("HOVR", "document"): 400}
    assert {timestamp(json.loads(line)) for line in output[:offset]} == {1000}
    assert {timestamp(json.loads(line)) for line in output[offset:]} == {2000}
    metrics.flush()
    assert len(output) == offset + 3  # Empty flush emits no duplicate counts.


def test_500_tickers_still_emit_only_five_metric_series():
    output = []
    metrics = Metrics(sink=output.append, environment="prod")
    call_types = ("submissions", "filing_index", "filing_directory", "document")
    for number in range(500):
        for call_type in call_types:
            metrics.count_sec_request(call_type, f"T{number}")
    metrics.flush()
    assert series(output) == {BASE: 2000, **{key(CallType=c): 500 for c in call_types}}
    assert len(details(output)) == 2000
    assert sum(details(output).values()) == 2000
    assert all("Ticker" not in json.loads(line) for line in output if "_aws" in json.loads(line))


@pytest.mark.parametrize("call_type,ticker", [("invalid", "AAPL"), ("document", "\n"),
    ("document", "x" * 1025), ("document", "\u2603")])
def test_invalid_dimension_values_do_not_partially_increment_total(call_type, ticker):
    output = []
    metrics = Metrics(sink=output.append)
    with pytest.raises(ValueError):
        metrics.count_sec_request(call_type, ticker)
    metrics.flush()
    assert not output
