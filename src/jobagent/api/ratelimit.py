"""A per-client token bucket, sized for a single-user self-hosted app.

Nothing bounded `/assistant/ask` (LLM spend) or `/ingest` (outbound fetching), which
is theoretical on a laptop and not theoretical the moment the port is reachable. These
limits exist to stop a runaway loop or an exposed port draining a quota — **not** to
police normal work, so the defaults are deliberately generous and exceeding one is a
sign something is wrong rather than that you are working hard.

In-process and per-worker on purpose. A shared store would mean Redis, which is a
second service to run for a single-user app — the wrong trade. The consequence worth
knowing: with N workers the effective limit is N x the configured one. Documented
rather than hidden, because a limit that silently means something else is worse than
none.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    tokens: float
    updated: float


@dataclass
class RateLimiter:
    """Classic token bucket: `per_hour` refills continuously, capacity == per_hour.

    Continuous refill rather than a fixed window because a window lets a caller spend
    the whole allowance in the last second of one window and again in the first second
    of the next — twice the intended rate at exactly the wrong moment.
    """

    per_hour: int
    clock: callable = time.monotonic          # injected so tests need no sleeping
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(self, key: str) -> tuple[bool, float]:
        """Spend one token for `key`. Returns (allowed, seconds_until_next_token)."""
        if self.per_hour <= 0:
            return True, 0.0                  # 0 disables the class entirely

        rate = self.per_hour / 3600.0
        now = self.clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                self._buckets[key] = _Bucket(tokens=self.per_hour - 1, updated=now)
                return True, 0.0

            bucket.tokens = min(
                float(self.per_hour), bucket.tokens + (now - bucket.updated) * rate)
            bucket.updated = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0.0
            return False, (1.0 - bucket.tokens) / rate

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


def client_key(request) -> str:
    """Identify the caller. Behind a reverse proxy every request shares one socket
    address, so the forwarded header is preferred when present — and only the FIRST
    hop is used, because the rest of that header is attacker-controlled.

    On the default loopback deployment this is one bucket for one person, which is
    exactly right.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"
