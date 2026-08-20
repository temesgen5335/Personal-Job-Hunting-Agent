"""Pre-flight: find out which providers actually work, before anything depends on it.

Failover is reactive — it discovers a dead provider by calling it and waiting for the
failure. That is correct and it is slow: a chain with two dead backends in front pays
both timeouts on *every* request, and the only symptom is latency nobody watches.

A probe is proactive. It calls every configured backend **concurrently**, once, with a
tight timeout, and records the result in the `Ledger`. Afterwards the router can order
by measured reality instead of a static preference list, and the operator can be told
plainly which keys are good.

Three deliberate constraints:

1. **Concurrent, not sequential.** Probing nine providers one at a time takes as long as
   the slowest chain; in parallel it takes as long as the slowest single provider.
2. **A real request, not a ping.** `llm_doctor --probe` once reported a provider healthy
   when it could not serve an actual call — "reply ok" fits in a quota where a real
   prompt does not. The probe sends a small but genuine completion.
3. **Never fatal.** A probe that raises would take down the thing it was meant to
   protect. Every failure becomes a recorded result.

Domain-agnostic: stdlib + the harness's own modules only (R30).
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field

from agentkit.llm.errors import Verdict, classify
from agentkit.llm.ledger import Ledger
from agentkit.llm.types import ChatRequest, Message

# A probe is diagnostics, not work: it must never cost more than a moment or more than
# a trivial number of tokens.
DEFAULT_TIMEOUT_S = 20.0
# NOT 16. Reasoning-style models (gpt-oss, o-series, Gemini thinking variants) spend
# output tokens on reasoning before emitting any visible text, so a tight budget returns
# stop_reason="length" with an EMPTY string — and a naive probe reads that as "provider
# down". Measured on groq/openai/gpt-oss-20b: 16 tokens → "", 64 → "ready" using 30.
# A diagnostic that condemns a healthy provider is as bad as one that clears a dead one;
# this one would have had someone rotating good API keys.
PROBE_MAX_TOKENS = 128
PROBE_SYSTEM = "You are a health check. Answer with a single word."
PROBE_USER = "Reply with the word: ready"


@dataclass(frozen=True)
class ProbeResult:
    provider: str
    model: str
    ok: bool
    latency_s: float
    verdict: str = ""
    detail: str = ""

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass
class ProbeReport:
    results: list[ProbeResult] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def working(self) -> list[ProbeResult]:
        """Fastest first — this is the routing order a caller should prefer."""
        return sorted((r for r in self.results if r.ok), key=lambda r: r.latency_s)

    @property
    def broken(self) -> list[ProbeResult]:
        return [r for r in self.results if not r.ok]

    @property
    def best(self) -> ProbeResult | None:
        return self.working[0] if self.working else None

    def as_dict(self) -> dict:
        return {
            "elapsed_s": round(self.elapsed_s, 3),
            "working": [r.label for r in self.working],
            "broken": [{"backend": r.label, "verdict": r.verdict, "detail": r.detail}
                       for r in self.broken],
        }

    def render(self) -> str:
        lines = [f"probed {len(self.results)} backend(s) in {self.elapsed_s:.1f}s"]
        for r in self.working:
            lines.append(f"  ✓ {r.label}  {r.latency_s:.2f}s")
        for r in self.broken:
            lines.append(f"  ✗ {r.label}  {r.verdict or 'error'} — {r.detail}")
        if not self.working:
            # The single most actionable thing this tool can say.
            lines.append("  NOTHING is reachable — check keys, network, and quotas.")
        return "\n".join(lines)


def probe_backend(backend, *, timeout_s: float = DEFAULT_TIMEOUT_S,
                  system: str = PROBE_SYSTEM, user: str = PROBE_USER,
                  max_tokens: int = PROBE_MAX_TOKENS) -> ProbeResult:
    """Call one backend once. Never raises."""
    provider = getattr(backend, "name", "?")
    model = getattr(backend, "model", "") or getattr(backend, "_model", "") or "?"
    started = time.monotonic()
    try:
        result = backend.chat(ChatRequest(system=system,
                                          messages=[Message("user", user)],
                                          max_tokens=max_tokens, timeout_s=timeout_s))
        text = result.text
        latency = time.monotonic() - started
        if not (text or "").strip():
            if getattr(result, "stop_reason", "") == "length":
                # The provider answered; WE gave it nowhere to answer in. Reporting this
                # as a provider fault would be blaming the wrong party, so it is called
                # out as a probe-budget problem and counted as reachable.
                return ProbeResult(provider, model, True, latency, "",
                                   f"reached, but {max_tokens} tokens was not enough "
                                   f"for a visible answer (reasoning model)")
            # A 200 with a genuinely empty body is a failure that looks like success —
            # how a misconfigured proxy passes a naive health check.
            return ProbeResult(provider, model, False, latency,
                               Verdict.UNKNOWN.value, "empty response")
        return ProbeResult(provider, model, True, latency)
    except Exception as exc:  # noqa: BLE001 — every failure is a result, never a crash
        latency = time.monotonic() - started
        classified = classify(exc)
        return ProbeResult(provider, model, False, latency,
                           classified.verdict.value, f"{type(exc).__name__}: {exc}")


def probe_all(backends, *, timeout_s: float = DEFAULT_TIMEOUT_S,
              ledger: Ledger | None = None, max_workers: int = 8,
              max_tokens: int = PROBE_MAX_TOKENS) -> ProbeReport:
    """Probe every backend concurrently and record the outcomes.

    `timeout_s` bounds each backend independently. A provider that hangs is reported as
    a timeout rather than being allowed to stall the whole report — which is the failure
    mode that would make an operator stop running this.
    """
    backends = list(backends)
    report = ProbeReport()
    if not backends:
        return report

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=min(max_workers, len(backends))) as pool:
        futures = {pool.submit(probe_backend, b, timeout_s=timeout_s,
                               max_tokens=max_tokens): b
                   for b in backends}
        for future, backend in futures.items():
            provider = getattr(backend, "name", "?")
            model = getattr(backend, "model", "") or getattr(backend, "_model", "") or "?"
            try:
                report.results.append(future.result(timeout=timeout_s))
            except FutureTimeout:
                report.results.append(ProbeResult(
                    provider, model, False, timeout_s, Verdict.TRANSIENT.value,
                    f"no response within {timeout_s:.0f}s"))
            except Exception as exc:  # noqa: BLE001
                report.results.append(ProbeResult(
                    provider, model, False, 0.0, Verdict.UNKNOWN.value,
                    f"{type(exc).__name__}: {exc}"))
    report.elapsed_s = time.monotonic() - started

    if ledger is not None:
        for r in report.results:
            ledger.record(r.provider, r.model, ok=r.ok, latency_s=r.latency_s,
                          verdict=r.verdict, detail=r.detail)
    return report


def order_by_health(backends, report: ProbeReport):
    """Reorder a chain so measured-working backends come first, fastest first.

    Unprobed backends keep their configured position *after* the known-good ones rather
    than being dropped: a probe is a snapshot, and a provider that failed one health
    check may still serve the next request. Known-dead ones go last, not away — this
    changes the ORDER, never the membership, so a probe can never leave the caller with
    an empty chain.
    """
    rank: dict[str, int] = {}
    for i, r in enumerate(report.working):
        rank[r.label] = i
    dead = {r.label for r in report.broken}

    def key(item):
        index, backend = item
        label = (f"{getattr(backend, 'name', '?')}/"
                 f"{getattr(backend, 'model', '') or getattr(backend, '_model', '') or '?'}")
        if label in rank:
            return (0, rank[label])
        if label in dead:
            return (2, index)
        return (1, index)

    return [b for _, b in sorted(enumerate(backends), key=key)]
