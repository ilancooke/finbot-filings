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

    def count(self, name, value=1):
        self.observe(name, value, unit="Count", aggregate=True)

    def observe(self, name, value, *, unit="Milliseconds", aggregate=False):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("metric must be finite numeric")
        with self._lock:
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
            self._counts, self._samples = {}, defaultdict(list)
        if not values:
            return
        event = {"_aws": {"Timestamp": self.now_ms(), "CloudWatchMetrics": [{
            "Namespace": self.namespace, "Dimensions": [["Service", "Environment"]],
            "Metrics": [{"Name": k, "Unit": units[k]} for k in values]}]},
            "Service": "finbot-ingestion", "Environment": self.environment, **values}
        self.sink(json.dumps(event, allow_nan=False, separators=(",", ":")))
