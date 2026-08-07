"""Walk the plan queue until the task is done.

The router already decided *which* backends can serve a task and *how*; this is what
actually walks that list. Three rules do most of the work:

1. **Failover follows the plan queue, not the provider chain.** An incapable backend
   never entered the queue, so a fallback can never land on a model that will silently
   do the task badly. This is the whole point of routing before running.

2. **Failures are classified before they are acted on.** A 429 is worth waiting out on
   the same backend; a 401 means move on and never come back this process. Falling
   through on every exception — what the old client did — turns a wrong API key into a
   full round trip on every future call.

3. **Budgets are not provider failures.** Hitting the wall clock or the tool ceiling
   raises straight out instead of failing over: retrying a task that is too expensive on
   a *different* model just spends the ceiling twice.

When the queue is exhausted the task's own `fallback` runs if it has one — a degraded
answer beats a dead pipeline — and only then does `NoCapableModel` surface, carrying
every rejection and every failure so the message says what to change.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from agentkit.llm.errors import Classified, Verdict, classify
from agentkit.llm.health import Breaker
from agentkit.llm.router import plans_for
from agentkit.llm.strategies import EXECUTORS, BudgetExceeded
from agentkit.llm.tasks import (
    NoCapableModel,
    Plan,
    Rejection,
    Strategy,
    TaskOutcome,
    TaskSpec,
)
from agentkit.llm.types import Usage
from agentkit.tools import ToolBox

# A rate limit whose Retry-After is longer than this is not worth waiting for — another
# backend will answer sooner.
MAX_INLINE_WAIT_S = 20.0


@dataclass
class Attempt:
    """One try on one plan. Kept for the audit trail and the failure message."""

    plan: Plan
    ok: bool
    elapsed_ms: int
    error: Classified | None = None


class _Recorder:
    """Wraps a backend so usage can be totalled without every strategy threading it back.

    Strategies return a string on purpose — that keeps them simple enough to read and to
    test. Token accounting is the Runner's business, so it happens here.
    """

    def __init__(self, backend):
        self._backend = backend
        self.usage = Usage()
        self.calls = 0

    name = property(lambda self: self._backend.name)
    model = property(lambda self: self._backend.model)
    card = property(lambda self: self._backend.card)

    def chat(self, request):
        self.calls += 1
        result = self._backend.chat(request)
        if result.usage:
            self.usage = Usage(self.usage.input_tokens + result.usage.input_tokens,
                               self.usage.output_tokens + result.usage.output_tokens)
        return result


@dataclass
class Runner:
    """Executes a `TaskSpec` against the best backend that can serve it."""

    backends: list = field(default_factory=list)
    toolbox: ToolBox = field(default_factory=ToolBox)
    breaker: Breaker = field(default_factory=Breaker)
    # Injected so tests never actually sleep and never actually read the clock.
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], float] = time.monotonic
    # Optional observability sink: on_event(kind, payload). Deliberately not required —
    # agentkit must stay usable with no host wiring at all.
    on_event: Callable[[str, dict], None] | None = None

    def _emit(self, kind: str, **payload) -> None:
        if self.on_event is not None:
            self.on_event(kind, payload)

    def run(self, task: TaskSpec, **inputs) -> TaskOutcome:
        started = self.now()
        plans, rejections = plans_for(task, self.backends, self.breaker)
        self._emit("plans", task=task.name,
                   plans=[repr(p) for p in plans],
                   rejected=[str(r) for r in rejections])

        attempts: list[Attempt] = []

        for plan in plans:
            if len(attempts) >= task.budget.max_attempts:
                rejections.append(Rejection(plan.backend.name, plan.backend.model,
                                            "attempt budget spent",
                                            f"{task.budget.max_attempts} attempts used"))
                break

            outcome = self._try(task, plan, inputs, attempts, rejections, started)
            if outcome is not None:
                return outcome

        # Every plan is spent. A deterministic answer is better than no answer.
        if task.fallback is not None:
            self._emit("fallback", task=task.name, attempts=len(attempts))
            return TaskOutcome(
                value=task.fallback(inputs),
                provider="none", model="none", tier=task.min_tier,
                strategy=Strategy.DETERMINISTIC_FALLBACK,
                attempts=len(attempts),
                elapsed_ms=int((self.now() - started) * 1000),
                warnings=("no model could serve this task; used the deterministic fallback",)
                         + tuple(str(a.error) for a in attempts if a.error),
            )

        raise NoCapableModel(task, rejections)

    def _try(self, task, plan, inputs, attempts, rejections, started) -> TaskOutcome | None:
        """Run one plan, retrying it in place only where that can help. None = move on."""
        executor = EXECUTORS[plan.strategy]

        while True:
            recorder = _Recorder(plan.backend)
            call_started = self.now()
            self._emit("attempt", task=task.name, backend=plan.backend.name,
                       model=plan.backend.model, strategy=str(plan.strategy))
            try:
                value = executor(task, recorder, self.toolbox, inputs, task.budget)
            except BudgetExceeded:
                # Not the provider's fault, and a different provider would cost the same.
                self._emit("budget_exceeded", task=task.name, backend=plan.backend.name)
                raise
            except Exception as exc:  # noqa: BLE001 — every provider error lands here
                verdict = classify(exc)
                elapsed = int((self.now() - call_started) * 1000)
                attempts.append(Attempt(plan, False, elapsed, verdict))
                # Recorded here rather than after the loop, so the failure message reads
                # in the order things actually happened.
                rejections.append(Rejection(plan.backend.name, plan.backend.model,
                                            "attempt failed", verdict.message))
                if verdict.should_cooldown:
                    self.breaker.record_failure(plan.backend.name, plan.backend.model,
                                                verdict)
                self._emit("attempt_failed", task=task.name, backend=plan.backend.name,
                           verdict=str(verdict.verdict), message=verdict.message)

                if self._retry_in_place(verdict, task, attempts):
                    continue
                return None                      # next plan in the queue

            elapsed = int((self.now() - call_started) * 1000)
            attempts.append(Attempt(plan, True, elapsed))
            self.breaker.record_success(plan.backend.name, plan.backend.model)
            self._emit("attempt_ok", task=task.name, backend=plan.backend.name,
                       elapsed_ms=elapsed)
            return TaskOutcome(
                value=value,
                provider=plan.backend.name,
                model=plan.backend.model,
                tier=plan.card.tier,
                strategy=plan.strategy,
                attempts=len(attempts),
                usage=recorder.usage,
                elapsed_ms=int((self.now() - started) * 1000),
                warnings=self._warnings(plan),
            )

    def _retry_in_place(self, verdict: Classified, task, attempts) -> bool:
        """Whether to try the SAME backend again.

        Only for failures the same backend can recover from, only with budget left, and
        only when the wait is shorter than moving on would be. A long `Retry-After` is a
        reason to switch backends, not to sit still.
        """
        if not verdict.retryable_same_backend:
            return False
        if len(attempts) >= task.budget.max_attempts:
            return False

        wait = verdict.retry_after_s
        if verdict.verdict is Verdict.RATE_LIMIT:
            if wait is None or wait > MAX_INLINE_WAIT_S:
                return False
        else:
            wait = min(wait or 1.0, MAX_INLINE_WAIT_S)

        if wait:
            self.sleep(wait)
        return True

    @staticmethod
    def _warnings(plan: Plan) -> tuple[str, ...]:
        """Surface the fact of degradation to the caller, which is what lets a UI say
        'answered by a weaker model' instead of quietly presenting a lesser answer as
        if it were the best available."""
        out = []
        if plan.level > 0:
            out.append(f"degraded: {plan.strategy} on {plan.backend.name}/"
                       f"{plan.backend.model} ({plan.card.tier.name.lower()})")
        if plan.card.source in ("size", "default"):
            out.append(f"{plan.backend.model} capabilities are inferred "
                       f"({plan.card.source}); set LLM_TIER_OVERRIDES to be sure")
        return tuple(out)
