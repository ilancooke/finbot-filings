"""Thread-safe bounded EMF emission; no metrics API calls in request threads."""

from collections import defaultdict
import json
import math
import threading
import time


class Metrics:
    def __init__(self, *, sink=print, namespace="Finbot/Ingestion", environment="local", now_ms=None):
        self.sink, self.namespace, self.environment = sink, namespace, environment
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._lock = threading.Lock()
        self._counts, self._samples, self._units = {}, defaultdict(list), {}
        self._sec_requests = defaultdict(int)

    def count(self, name, value=1):
        self.observe(name, value, unit="Count", aggregate=True)

    def count_sec_request(self, call_type, ticker=None):
        """Count one dispatched HTTP attempt in the total and each breakdown."""
        if call_type not in {"submissions", "filing_index", "filing_directory", "document"}:
            raise ValueError("invalid SEC call type")
        ticker = ticker or "UNKNOWN"
        if (not isinstance(ticker, str) or not 1 <= len(ticker) <= 1024
                or not ticker.strip() or any(not 32 <= ord(c) <= 126 for c in ticker)):
            raise ValueError("invalid SEC metric ticker")
        with self._lock:
            self._observe("SecRequests", 1, unit="Count", aggregate=True)
            self._sec_requests[(ticker, call_type)] += 1

    def observe(self, name, value, *, unit="Milliseconds", aggregate=False):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("metric must be finite numeric")
        with self._lock:
            self._observe(name, value, unit=unit, aggregate=aggregate)

    def _observe(self, name, value, *, unit, aggregate):
        # Caller holds the lock, including atomic SEC total/breakdown updates.
        if name not in self._units and len(self._units) >= 99:
            raise ValueError("metric name bound exceeded")
        if name in self._units and self._units[name] != unit:
            raise ValueError("metric unit changed")
        self._units[name] = unit
        if aggregate:
            self._counts[name] = self._counts.get(name, 0) + value
        elif len(self._samples[name]) < 100:
            self._samples[name].append(value)
        else:
            self._counts["MetricSamplesDropped"] = self._counts.get("MetricSamplesDropped", 0) + 1
            self._units["MetricSamplesDropped"] = "Count"

    def latency(self, name, start, end):
        delta = (end - start).total_seconds() * 1000
        if delta < 0:
            self.count("ClockAnomalies")
        else:
            self.observe(name, delta)

    def flush(self):
        with self._lock:
            values = {**self._counts, **{k: v for k, v in self._samples.items() if v}}
            units = dict(self._units)
            sec_requests = self._sec_requests
            self._counts, self._samples = {}, defaultdict(list)
            self._sec_requests = defaultdict(int)
        if not values:
            return
        timestamp = self.now_ms()
        event = {"_aws": {"Timestamp": timestamp, "CloudWatchMetrics": [{
            "Namespace": self.namespace, "Dimensions": [["Service", "Environment"]],
            "Metrics": [{"Name": k, "Unit": units[k]} for k in values]}]},
            "Service": "finbot-ingestion", "Environment": self.environment, **values}
        self.sink(json.dumps(event, allow_nan=False, separators=(",", ":")))
        for (ticker, call_type), count in sec_requests.items():
            # The base event alone emits the total; including its dimension set
            # here would count each request a second time.
            event = {"_aws": {"Timestamp": timestamp, "CloudWatchMetrics": [{
                "Namespace": self.namespace, "Dimensions": [
                    ["Service", "Environment", "Ticker"],
                    ["Service", "Environment", "CallType"],
                    ["Service", "Environment", "Ticker", "CallType"]],
                "Metrics": [{"Name": "SecRequests", "Unit": "Count"}]}]},
                "Service": "finbot-ingestion", "Environment": self.environment,
                "Ticker": ticker, "CallType": call_type, "SecRequests": count}
            self.sink(json.dumps(event, allow_nan=False, separators=(",", ":")))
