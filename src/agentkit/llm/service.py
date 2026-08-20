"""`LLMService` — the one object a host application needs.

Everything under `agentkit/llm/` is already domain-agnostic and independently useful, but
using it well meant knowing about six modules: build the chain, resolve capability cards,
consult the breaker, classify errors, walk the plan queue. That is a lot of surface for
"give me an answer from whichever provider is up".

This is the facade. It owns a chain, a breaker, a ledger and an optional probe, and
exposes three things:

    service = LLMService.from_settings(settings)      # any object with the attrs
    service.preflight()                               # optional: measure, then reorder
    service.complete(system, user)                    # answer, with failover

**Reusable by construction, not by intention.** It imports stdlib, pydantic-free, and
nothing from any host application (R30, enforced by tests). `from_settings` reads
attributes off *any* object with the right names — a pydantic Settings, a dataclass, an
argparse Namespace, a SimpleNamespace built from `os.environ`. `from_providers` skips
settings entirely and takes explicit descriptors, so a different project can supply its
own provider table without forking this one.

What it adds over a plain failover loop:

- **Pre-flight**: probe every provider concurrently, then route by measured latency
  instead of a static preference order.
- **Memory**: a dead provider is skipped rather than retried on every call (the breaker).
- **Classification**: a 401 is permanent, a 429 is not, and a malformed tool call is
  neither — they are handled differently rather than all becoming "try the next one".
- **A trace**: which backends work, which do not, each failure by verdict, queryable.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from agentkit.llm.chain import DEFAULT_ORDER, DEFAULT_PROVIDERS, ProviderSpec, build_chain
from agentkit.llm.errors import Verdict, classify
from agentkit.llm.health import Breaker
from agentkit.llm.ledger import Ledger
from agentkit.llm.probe import ProbeReport, order_by_health, probe_all
from agentkit.llm.types import ChatRequest, Message

logger = logging.getLogger(__name__)


class AllProvidersFailed(RuntimeError):
    """Every backend was tried and none answered.

    Carries the per-provider verdicts, because "all providers failed" on its own sends
    an operator hunting a code fault when the real cause is usually one expired key or
    an exhausted daily quota.
    """

    def __init__(self, attempts: list[tuple[str, str, str]]):
        self.attempts = attempts
        detail = "; ".join(f"{p}/{m}: {v}" for p, m, v in attempts) or "no backends configured"
        super().__init__(f"All LLM providers failed — {detail}")


@dataclass
class LLMService:
    """A multi-provider LLM with health-aware routing and a queryable trace."""

    backends: list = field(default_factory=list)
    breaker: Breaker = field(default_factory=Breaker)
    ledger: Ledger = field(default_factory=Ledger)
    skipped: list = field(default_factory=list)     # configured but unusable, with reasons

    # --- construction ---------------------------------------------------------

    @classmethod
    def from_settings(cls, settings, *, providers=DEFAULT_PROVIDERS,
                      order=DEFAULT_ORDER, primary: str = "") -> "LLMService":
        """Build from any object exposing the descriptor's field names.

        Deliberately duck-typed: `getattr(settings, "groq_api_key", "")`. A host with a
        different config class supplies its own `providers` table rather than adapting
        its settings to ours.
        """
        # report=True returns the ChainReport, which carries WHY a provider was left
        # out. That reason is most of the value of this constructor: "no chain" and
        # "no GROQ_API_KEY" are the same symptom with very different fixes.
        report = build_chain(settings, providers=providers, order=order,
                             primary=primary, report=True)
        return cls(backends=list(report.backends), skipped=list(report.skipped))

    @classmethod
    def from_providers(cls, specs: list[ProviderSpec], settings, **kwargs) -> "LLMService":
        """Explicit provider table — for a host that does not want ours at all."""
        return cls.from_settings(settings, providers=tuple(specs), **kwargs)

    # --- introspection --------------------------------------------------------

    @property
    def chain(self) -> list[str]:
        return [getattr(b, "name", "?") for b in self.backends]

    def describe(self) -> str:
        """Human-readable state: the order, then the measured health of each backend."""
        lines = [f"chain: {' → '.join(self.chain) or '(empty)'}"]
        for name, reason in self.skipped:
            lines.append(f"  skipped {name}: {reason}")
        lines.append(self.ledger.render())
        return "\n".join(lines)

    # --- pre-flight -----------------------------------------------------------

    def preflight(self, *, timeout_s: float = 20.0, reorder: bool = True) -> ProbeReport:
        """Probe every backend concurrently and (by default) reorder by what answered.

        Costs one tiny call per provider. Worth it before a long batch — a chain with
        two dead backends in front otherwise pays both timeouts on every request in it.

        Reordering never changes membership, only sequence, so this cannot leave the
        service with nothing to call even if every probe fails.
        """
        report = probe_all(self.backends, timeout_s=timeout_s, ledger=self.ledger)
        if reorder and report.results:
            self.backends = order_by_health(self.backends, report)
            logger.info("preflight: %d up, %d down — order now %s",
                        len(report.working), len(report.broken), " → ".join(self.chain))
        return report

    # --- the actual call ------------------------------------------------------

    def complete(self, system: str, user: str, *, json_mode: bool = False,
                 max_tokens: int = 4096, timeout_s: float = 60.0) -> str:
        """First backend that answers wins. Raises AllProvidersFailed if none do.

        Speaks the backend seam's own protocol — `chat(ChatRequest) -> ChatResult`.
        An earlier draft called a `generate(system, user)` method that does not
        exist on any real backend; the tests passed because the fake was written to
        match the assumption rather than the interface. Hence
        `test_the_service_speaks_the_same_protocol_real_backends_expose`.
        """
        if json_mode:
            system = (system + "\nReturn ONLY valid JSON — no markdown, no code fences, "
                               "no prose.")
        attempts: list[tuple[str, str, str]] = []

        for backend in list(self.backends):
            name = getattr(backend, "name", "?")
            model = getattr(backend, "model", "") or getattr(backend, "_model", "") or "?"

            if self.breaker.is_open(name, model):
                # Skipping is the whole point of having memory: a provider with a bad
                # key costs a full round trip on every call otherwise.
                wait = self.breaker.opens_in(name, model)
                attempts.append((name, model, f"skipped (cooling down {wait:.0f}s)"))
                continue

            started = time.monotonic()
            try:
                result = backend.chat(ChatRequest(
                    system=system, messages=[Message("user", user)],
                    max_tokens=max_tokens, timeout_s=timeout_s))
                text = result.text
                if not (text or "").strip():
                    raise RuntimeError("empty response")
            except Exception as exc:  # noqa: BLE001 — trying the next one is the design
                latency = time.monotonic() - started
                verdict = classify(exc)
                self.ledger.record(name, model, ok=False, latency_s=latency,
                                   verdict=verdict.verdict,
                                   detail=f"{type(exc).__name__}: {exc}")
                self._trip(name, model, verdict)
                attempts.append((name, model, verdict.verdict.value))
                logger.warning("LLM %s/%s failed (%s) — trying next",
                               name, model, verdict.verdict.value)
                continue

            latency = time.monotonic() - started
            self.ledger.record(name, model, ok=True, latency_s=latency)
            self._recover(name, model)
            if backend is not self.backends[0]:
                logger.warning("LLM failover: served by %s/%s", name, model)
            return _strip_fences(text) if json_mode else text

        raise AllProvidersFailed(attempts)

    # --- breaker glue ---------------------------------------------------------
    #
    # Thin wrappers, so a host can substitute its own policy object by matching two
    # method names rather than reading `complete`.

    def _trip(self, name: str, model: str, classified) -> None:
        self.breaker.record_failure(name, model, classified)

    def _recover(self, name: str, model: str) -> None:
        self.breaker.record_success(name, model)


def _strip_fences(text: str) -> str:
    """Models wrap JSON in ```json fences despite being told not to."""
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned
        if cleaned.endswith("```"):
            cleaned = cleaned[: cleaned.rfind("```")]
    return cleaned.strip()
