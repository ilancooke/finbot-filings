"""SEC HTTP attempts reconcile across CloudWatch's four dimension sets."""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
import requests

from finbot_ingestion.domain import Company, Filing
from finbot_ingestion.observability.metrics import Metrics
from finbot_ingestion.sec.errors import SECRequestStopped
from finbot_ingestion.sec.urls import filing_index_url
from test_ingestion_transport import make, Response, URL


BASE = (("Environment", "prod"), ("Service", "finbot-ingestion"))


def series(output):
    """Extract and sum each series exactly as EMF dimension sets specify."""
    counts = defaultdict(int)
    for line in output:
        event = json.loads(line)
        for directive in event["_aws"]["CloudWatchMetrics"]:
            assert directive["Namespace"] == "Finbot/Ingestion"
            for metric in directive["Metrics"]:
                if metric["Name"] == "SecRequests":
                    assert metric["Unit"] == "Count"
                    for dimensions in directive["Dimensions"]:
                        key = tuple(sorted((name, event[name]) for name in dimensions))
                        counts[key] += event["SecRequests"]
    return counts


def key(**dimensions):
    return tuple(sorted((*BASE, *dimensions.items())))


def test_seven_http_attempts_reconcile_independent_and_combined_breakdowns():
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
    assert counts[key(Ticker="AAPL")] == 4
    assert counts[key(Ticker="HOVR")] == 3
    for call_type, expected in (("submissions", 2), ("filing_index", 2),
                                ("filing_directory", 2), ("document", 1)):
        assert counts[key(CallType=call_type)] == expected
    expected_pairs = {(ticker, call_type): 1 for ticker in ("AAPL", "HOVR")
                      for call_type in ("submissions", "filing_index", "filing_directory")}
    expected_pairs[("AAPL", "document")] = 1
    assert {tuple(dict(k)[name] for name in ("Ticker", "CallType")): v
            for k, v in counts.items() if len(k) == 4} == expected_pairs
    for dimensions in (3, 4):
        # The 3-dimension sets include both independent views.
        assert sum(v for k, v in counts.items() if len(k) == dimensions) == (14 if dimensions == 3 else 7)
    events = [json.loads(line) for line in output]
    assert {event["_aws"]["Timestamp"] for event in events} == {123}
    assert events[0]["RuntimeHealthy"] == 1
    assert all(directive["Metrics"] == [{"Name": "SecRequests", "Unit": "Count"}]
               for event in events[1:] for directive in event["_aws"]["CloudWatchMetrics"])


def test_network_retry_and_redirect_each_count_under_original_company_and_type():
    output = []
    client, session, _ = make([requests.Timeout("timeout"),
        Response(status=302, headers={"Location": "/Archives/next"}), Response()])
    client.metrics = Metrics(sink=output.append, environment="prod")
    client.download_document(URL, context={"ticker": "AAPL"})
    client.metrics.flush()
    counts = series(output)
    assert len(session.calls) == counts[BASE] == 3
    assert counts[key(Ticker="AAPL")] == counts[key(CallType="document")] == 3
    assert counts[key(Ticker="AAPL", CallType="document")] == 3
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
    assert len(session.calls) == counts[BASE] == counts[key(Ticker="AAPL")] == 3
    for call_type in ("submissions", "filing_index", "filing_directory"):
        assert counts[key(Ticker="AAPL", CallType=call_type)] == 1


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
    assert counts[key(Ticker="UNKNOWN", CallType="document")] == 1
    assert key(Ticker="AAPL") not in counts


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
    assert first[BASE] == first[key(Ticker="AAPL", CallType="submissions")] == 1
    assert key(Ticker="HOVR") not in first
    offset = len(output)
    metrics.flush()
    second = series(output[offset:])
    assert second[BASE] == second[key(Ticker="HOVR", CallType="document")] == 400
    assert key(Ticker="AAPL") not in second
    assert {json.loads(line)["_aws"]["Timestamp"] for line in output[:offset]} == {1000}
    assert {json.loads(line)["_aws"]["Timestamp"] for line in output[offset:]} == {2000}
    metrics.flush()
    assert len(output) == offset + 2  # Empty flush emits no duplicate counts.


@pytest.mark.parametrize("call_type,ticker", [("invalid", "AAPL"), ("document", "\n"),
    ("document", "x" * 1025), ("document", "\u2603")])
def test_invalid_dimension_values_do_not_partially_increment_total(call_type, ticker):
    output = []
    metrics = Metrics(sink=output.append)
    with pytest.raises(ValueError):
        metrics.count_sec_request(call_type, ticker)
    metrics.flush()
    assert not output
