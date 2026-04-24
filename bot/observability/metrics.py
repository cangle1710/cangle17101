"""Minimal Prometheus text-format metrics registry.

Stdlib-only; no `prometheus_client` dependency. We support three types:

  - Counter: monotonically increasing total with optional labels.
  - Gauge:   single numeric value, settable up or down.
  - Histogram: predefined buckets + sum + count.

Exposed via `MetricsRegistry.render()` as a `text/plain; version=0.0.4`
body suitable for `/metrics` scraping by Prometheus or Grafana Cloud.

This is intentionally tiny. For richer features (pushgateway, exemplars,
OpenMetrics histograms with quantiles), swap in `prometheus_client`. The
code here is careful enough that the migration would be mechanical.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Iterable, Optional


LabelKey = tuple[tuple[str, str], ...]  # sorted tuple of (name, value)


def _normalize_labels(labels: Optional[dict[str, str]]) -> LabelKey:
    if not labels:
        return ()
    return tuple(sorted((k, str(v)) for k, v in labels.items()))


def _escape_label(v: str) -> str:
    # Prometheus text format: escape backslash, newline, and double quote.
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_labels(lk: LabelKey) -> str:
    if not lk:
        return ""
    return "{" + ",".join(f'{k}="{_escape_label(v)}"' for k, v in lk) + "}"


class _Metric:
    def __init__(self, name: str, help_: str, kind: str):
        self.name = name
        self.help = help_
        self.kind = kind
        self._lock = threading.Lock()

    def _header(self) -> str:
        return f"# HELP {self.name} {self.help}\n# TYPE {self.name} {self.kind}\n"


class Counter(_Metric):
    def __init__(self, name: str, help_: str, labelnames: Iterable[str] = ()):
        super().__init__(name, help_, "counter")
        self._labelnames = tuple(labelnames)
        self._values: dict[LabelKey, float] = {}

    def inc(self, value: float = 1.0, labels: Optional[dict[str, str]] = None) -> None:
        if value < 0:
            raise ValueError("Counter.inc requires non-negative value")
        lk = _normalize_labels(labels)
        with self._lock:
            self._values[lk] = self._values.get(lk, 0.0) + value

    def render(self) -> str:
        out = [self._header()]
        with self._lock:
            items = list(self._values.items())
        for lk, v in items:
            out.append(f"{self.name}{_format_labels(lk)} {v}\n")
        return "".join(out)


class Gauge(_Metric):
    def __init__(self, name: str, help_: str, labelnames: Iterable[str] = ()):
        super().__init__(name, help_, "gauge")
        self._labelnames = tuple(labelnames)
        self._values: dict[LabelKey, float] = {}

    def set(self, value: float, labels: Optional[dict[str, str]] = None) -> None:
        lk = _normalize_labels(labels)
        with self._lock:
            self._values[lk] = float(value)

    def inc(self, value: float = 1.0, labels: Optional[dict[str, str]] = None) -> None:
        lk = _normalize_labels(labels)
        with self._lock:
            self._values[lk] = self._values.get(lk, 0.0) + value

    def dec(self, value: float = 1.0, labels: Optional[dict[str, str]] = None) -> None:
        self.inc(-value, labels=labels)

    def render(self) -> str:
        out = [self._header()]
        with self._lock:
            items = list(self._values.items())
        for lk, v in items:
            out.append(f"{self.name}{_format_labels(lk)} {v}\n")
        return "".join(out)


_DEFAULT_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)


class Histogram(_Metric):
    def __init__(
        self,
        name: str,
        help_: str,
        buckets: Iterable[float] = _DEFAULT_BUCKETS,
        labelnames: Iterable[str] = (),
    ):
        super().__init__(name, help_, "histogram")
        self._labelnames = tuple(labelnames)
        # Each label-key maps to (bucket_counts[list], sum, count).
        self._buckets = tuple(sorted(buckets))
        self._series: dict[LabelKey, list[float]] = {}

    def observe(self, value: float, labels: Optional[dict[str, str]] = None) -> None:
        lk = _normalize_labels(labels)
        with self._lock:
            series = self._series.get(lk)
            if series is None:
                # buckets + sum + count
                series = [0.0] * (len(self._buckets) + 2)
                self._series[lk] = series
            for i, b in enumerate(self._buckets):
                if value <= b:
                    series[i] += 1
            # sum is second-to-last, count is last
            series[-2] += value
            series[-1] += 1

    def render(self) -> str:
        out = [self._header()]
        with self._lock:
            items = [(lk, list(s)) for lk, s in self._series.items()]
        for lk, series in items:
            label_list = list(lk)
            for i, b in enumerate(self._buckets):
                bucket_labels = tuple(label_list + [("le", str(b))])
                out.append(
                    f"{self.name}_bucket{_format_labels(bucket_labels)} {series[i]}\n"
                )
            bucket_labels = tuple(label_list + [("le", "+Inf")])
            out.append(
                f"{self.name}_bucket{_format_labels(bucket_labels)} {series[-1]}\n"
            )
            out.append(f"{self.name}_sum{_format_labels(lk)} {series[-2]}\n")
            out.append(f"{self.name}_count{_format_labels(lk)} {series[-1]}\n")
        return "".join(out)


class MetricsRegistry:
    def __init__(self):
        self._metrics: list[_Metric] = []
        self._by_name: dict[str, _Metric] = {}
        self._lock = threading.Lock()

    def register(self, metric: _Metric) -> _Metric:
        with self._lock:
            if metric.name in self._by_name:
                # idempotent re-register returns the existing one
                return self._by_name[metric.name]
            self._by_name[metric.name] = metric
            self._metrics.append(metric)
        return metric

    def counter(self, name: str, help_: str,
                labelnames: Iterable[str] = ()) -> Counter:
        existing = self._by_name.get(name)
        if existing is not None:
            assert isinstance(existing, Counter), \
                f"{name} already registered as {existing.kind}"
            return existing
        return self.register(Counter(name, help_, labelnames))  # type: ignore[return-value]

    def gauge(self, name: str, help_: str,
              labelnames: Iterable[str] = ()) -> Gauge:
        existing = self._by_name.get(name)
        if existing is not None:
            assert isinstance(existing, Gauge), \
                f"{name} already registered as {existing.kind}"
            return existing
        return self.register(Gauge(name, help_, labelnames))  # type: ignore[return-value]

    def histogram(
        self,
        name: str,
        help_: str,
        buckets: Iterable[float] = _DEFAULT_BUCKETS,
        labelnames: Iterable[str] = (),
    ) -> Histogram:
        existing = self._by_name.get(name)
        if existing is not None:
            assert isinstance(existing, Histogram), \
                f"{name} already registered as {existing.kind}"
            return existing
        return self.register(Histogram(name, help_, buckets, labelnames))  # type: ignore[return-value]

    def render(self) -> str:
        with self._lock:
            metrics = list(self._metrics)
        return "".join(m.render() for m in metrics)

    def clear(self) -> None:
        """For tests."""
        with self._lock:
            self._metrics.clear()
            self._by_name.clear()


# Module-level default registry.
registry = MetricsRegistry()
