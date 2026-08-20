"""A trace log of what each provider actually did.

The circuit breaker in `health.py` knows whether a backend is *currently* open. That is
enough to route around a dead provider and not enough to answer the question an operator
actually asks: **which of my providers work, which do not, and why?**

Two dead model slugs sat in a live chain for weeks precisely because nothing answered
that. The answers still arrived — from the next provider, a little slower — so the only
symptom was latency, and latency is not something anyone watches.

This records every attempt: provider, model, outcome, classified error verdict, and how
long it took. In-process and bounded, like the breaker: a restart is a legitimate fresh
start, and a persisted verdict would outlive the fixed API key that caused it.

Domain-agnostic — stdlib only, no provider SDKs, no host imports (R30).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from agentkit.llm.errors import Verdict

# Per (provider, model). Enough to see a pattern, small enough to never matter for RAM.
MAX_EVENTS = 50


@dataclass(frozen=True)
class Attempt:
    """One call to one backend."""

    provider: str
    model: str
    ok: bool
    latency_s: float
    verdict: str = ""            # Verdict value when not ok
    detail: str = ""             # short, human-readable; never the full payload
    at: float = 0.0              # wall clock, for display


@dataclass
class ProviderStats:
    """Rolled-up health for one (provider, model)."""

    provider: str
    model: str
    calls: int = 0
    failures: int = 0
    total_latency_s: float = 0.0
    by_verdict: dict[str, int] = field(default_factory=dict)
    last_error: str = ""
    last_ok_at: float = 0.0
    last_fail_at: float = 0.0

    @property
    def successes(self) -> int:
        return self.calls - self.failures

    @property
    def success_rate(self) -> float | None:
        """None, not 1.0, when nothing has been attempted.

        A vacuous 100% is how a provider that has never been called reads as healthy —
        the same trap the assistant eval hit when it printed "grounding 100%" over zero
        graded cases.
        """
        return None if not self.calls else self.successes / self.calls

    @property
    def avg_latency_s(self) -> float | None:
        return None if not self.calls else self.total_latency_s / self.calls

    @property
    def health(self) -> str:
        """A word an operator can act on."""
        if not self.calls:
            return "untried"
        if self.failures == 0:
            return "ok"
        if self.successes == 0:
            # Every call failed. If the reason never changes it is a config problem,
            # not weather — worth naming differently from "flaky".
            return "dead"
        return "degraded"


@dataclass
class Ledger:
    """Thread-safe record of provider attempts.

    Injected `now` so tests are deterministic rather than timing real calls.
    """

    now: object = field(default=None)
    _stats: dict[tuple[str, str], ProviderStats] = field(default_factory=dict)
    _events: dict[tuple[str, str], list[Attempt]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        if self.now is None:
            self.now = time.time

    def record(self, provider: str, model: str, *, ok: bool, latency_s: float,
               verdict: Verdict | str = "", detail: str = "") -> Attempt:
        key = (provider, model)
        verdict_value = getattr(verdict, "value", verdict) or ""
        attempt = Attempt(provider=provider, model=model, ok=ok,
                          latency_s=round(latency_s, 3), verdict=verdict_value,
                          detail=_short(detail), at=self.now())
        with self._lock:
            stats = self._stats.setdefault(key, ProviderStats(provider, model))
            stats.calls += 1
            stats.total_latency_s += latency_s
            if ok:
                stats.last_ok_at = attempt.at
            else:
                stats.failures += 1
                stats.last_fail_at = attempt.at
                stats.last_error = f"{verdict_value}: {attempt.detail}".strip(": ")
                stats.by_verdict[verdict_value] = stats.by_verdict.get(verdict_value, 0) + 1

            events = self._events.setdefault(key, [])
            events.append(attempt)
            if len(events) > MAX_EVENTS:
                del events[: len(events) - MAX_EVENTS]
        return attempt

    def stats(self) -> list[ProviderStats]:
        """Every tracked backend, worst health first — the thing to look at is on top."""
        order = {"dead": 0, "degraded": 1, "untried": 2, "ok": 3}
        with self._lock:
            return sorted(self._stats.values(),
                          key=lambda s: (order.get(s.health, 9), -s.failures))

    def events(self, provider: str = "", model: str = "") -> list[Attempt]:
        with self._lock:
            out: list[Attempt] = []
            for (prov, mod), events in self._events.items():
                if provider and prov != provider:
                    continue
                if model and mod != model:
                    continue
                out.extend(events)
        return sorted(out, key=lambda a: a.at)

    def working(self) -> list[tuple[str, str]]:
        """Backends that have actually served a request. The positive list matters as
        much as the failure list: "which of these can I rely on right now"."""
        return [(s.provider, s.model) for s in self.stats() if s.successes > 0]

    def broken(self) -> list[tuple[str, str]]:
        return [(s.provider, s.model) for s in self.stats() if s.health == "dead"]

    def as_dict(self) -> dict:
        """Serialisable snapshot — for a run-ledger event, an API response, or a log."""
        return {
            "backends": [
                {
                    "provider": s.provider, "model": s.model, "health": s.health,
                    "calls": s.calls, "failures": s.failures,
                    "success_rate": s.success_rate,
                    "avg_latency_s": None if s.avg_latency_s is None
                    else round(s.avg_latency_s, 3),
                    "by_verdict": dict(s.by_verdict),
                    "last_error": s.last_error,
                }
                for s in self.stats()
            ],
            "working": [f"{p}/{m}" for p, m in self.working()],
            "broken": [f"{p}/{m}" for p, m in self.broken()],
        }

    def render(self) -> str:
        """One line per backend, aligned, worst first. Written for a terminal — this is
        what `make doctor` prints and what someone pastes into a bug report."""
        rows = self.stats()
        if not rows:
            return "no provider calls recorded yet"
        mark = {"ok": "✓", "degraded": "~", "dead": "✗", "untried": "·"}
        width = max(len(f"{s.provider}/{s.model}") for s in rows)
        lines = []
        for s in rows:
            name = f"{s.provider}/{s.model}".ljust(width)
            rate = "  n/a" if s.success_rate is None else f"{s.success_rate:>4.0%}"
            latency = "    —" if s.avg_latency_s is None else f"{s.avg_latency_s:>4.2f}s"
            line = f"{mark.get(s.health, '?')} {name}  {rate}  {latency}  {s.calls:>3} call(s)"
            if s.last_error:
                line += f"  — {s.last_error}"
            lines.append(line)
        return "\n".join(lines)


def _short(text: str, limit: int = 120) -> str:
    """Errors go into logs and API responses, so they are trimmed and single-lined.

    Provider error bodies can be kilobytes of JSON, and one of them pasted whole into a
    tool result is how a model ends up quoting an API key back at someone.
    """
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
