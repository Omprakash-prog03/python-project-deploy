"""Token-bucket rate limiting.

In-process and therefore per worker: with N workers or replicas the effective
limit is N x the configured budget. That is fine for abuse protection on a
single instance; a shared Redis-backed limiter is required for strict global
quotas (see docs/DEPLOYMENT.md).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    tokens: float
    updated: float = field(default_factory=time.monotonic)


class TokenBucketLimiter:
    def __init__(self, per_minute: int = 240, burst: int = 60) -> None:
        self.rate = max(1.0, per_minute / 60.0)  # tokens per second
        self.capacity = float(max(burst, 1))
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, cost: float = 1.0) -> tuple[bool, float, int]:
        """Return ``(allowed, retry_after_seconds, remaining)``."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self.capacity, updated=now)
                self._buckets[key] = bucket
                if len(self._buckets) > 20_000:  # crude memory guard
                    self._prune(now)
            elapsed = now - bucket.updated
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.rate)
            bucket.updated = now
            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True, 0.0, int(bucket.tokens)
            deficit = cost - bucket.tokens
            return False, round(deficit / self.rate, 2), 0

    def _prune(self, now: float) -> None:
        stale = [k for k, b in self._buckets.items() if now - b.updated > 600]
        for k in stale:
            self._buckets.pop(k, None)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
