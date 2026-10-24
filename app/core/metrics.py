"""Lightweight in-process metrics with a Prometheus exposition endpoint.

Deliberately dependency free: counters, gauges and latency reservoirs are
enough to power dashboards and health alerting for a single-region service.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Iterable

_LOCK = threading.Lock()


class Metrics:
    def __init__(self) -> None:
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._gauges: dict[str, float] = {}
        self._latencies: dict[str, list[float]] = defaultdict(list)
        self._started_at = time.time()

    # ------------------------------------------------------------- recording
    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with _LOCK:
            self._counters[key] += value

    def gauge(self, name: str, value: float) -> None:
        with _LOCK:
            self._gauges[name] = value

    def observe(self, name: str, seconds: float) -> None:
        with _LOCK:
            bucket = self._latencies[name]
            bucket.append(seconds)
            if len(bucket) > 2000:
                del bucket[:1000]

    # ------------------------------------------------------------- reporting
    def snapshot(self) -> dict[str, object]:
        with _LOCK:
            counters = {
                self._label_key(name, labels): round(value, 4)
                for (name, labels), value in self._counters.items()
            }
            latency = {
                name: {
                    "count": len(values),
                    "p50_ms": round(_percentile(values, 50) * 1000, 2),
                    "p95_ms": round(_percentile(values, 95) * 1000, 2),
                    "p99_ms": round(_percentile(values, 99) * 1000, 2),
                    "avg_ms": round((sum(values) / len(values)) * 1000, 2) if values else 0,
                }
                for name, values in self._latencies.items()
            }
            return {
                "uptime_seconds": round(time.time() - self._started_at, 1),
                "counters": counters,
                "gauges": dict(self._gauges),
                "latency": latency,
            }

    def prometheus(self) -> str:
        lines: list[str] = []
        with _LOCK:
            for (name, labels), value in sorted(self._counters.items()):
                label_str = _render_labels(labels)
                lines.append(f"platevision_{name}_total{label_str} {value}")
            for name, value in sorted(self._gauges.items()):
                lines.append(f"platevision_{name} {value}")
            for name, values in sorted(self._latencies.items()):
                if not values:
                    continue
                for q in (50, 95, 99):
                    lines.append(
                        f'platevision_{name}_seconds{{quantile="{q / 100}"}} '
                        f"{_percentile(values, q):.6f}"
                    )
                lines.append(f"platevision_{name}_count {len(values)}")
        lines.append(f"platevision_uptime_seconds {time.time() - self._started_at:.0f}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _label_key(name: str, labels: Iterable[tuple[str, str]]) -> str:
        label_str = ",".join(f"{k}={v}" for k, v in labels)
        return f"{name}{{{label_str}}}" if label_str else name


def _render_labels(labels: Iterable[tuple[str, str]]) -> str:
    items = list(labels)
    if not items:
        return ""
    return "{" + ",".join(f'{k}="{v}"' for k, v in items) + "}"


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, round((pct / 100) * (len(ordered) - 1))))
    return ordered[idx]


metrics = Metrics()
