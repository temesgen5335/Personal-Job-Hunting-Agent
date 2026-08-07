"""The governed tool seam: policy and audit wrapped around execution.

`GuardedToolBox` has the same shape as `ToolBox` — `specs()` and `execute()` — so it
drops straight into the `Runner`. The Runner never learns that permissions exist, which
means there is no ungoverned path: you cannot accidentally run the agent loop against
the raw toolbox, because the loop only ever holds whichever box it was given, and the
host wires the guarded one.

The order inside `execute()` is the safety property, and it is fixed:

    1. audit the INTENT          (before anything can refuse it)
    2. check the allow-list      (per-turn narrowing)
    3. ask the gatekeeper
    4. audit the DECISION
    5. run, or return a refusal the model can read
    6. audit the RESULT

Step 1 before step 3 is the point. A trail that only records permitted calls cannot
tell you the agent tried to rewrite its own configuration and was stopped — which is
the single line you most want after something goes wrong.

A refusal is returned as a tool *result*, not raised. The model should see "you may not
do that, here is why" and carry on with what it can do; an exception would discard the
turn and tell the operator nothing.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from agentkit.audit import Auditor
from agentkit.permissions import Decision, Gatekeeper, Permission, ToolPolicy
from agentkit.session import SessionContext
from agentkit.tools import ToolBox
from agentkit.llm.types import ToolCall, ToolResult, ToolSpec


@dataclass
class GuardedToolBox:
    """A ToolBox that answers to a policy and writes a trail."""

    inner: ToolBox
    gate: Gatekeeper
    audit: Auditor
    context: SessionContext = field(default_factory=SessionContext)
    # Per-turn narrowing. None = every registered tool. Set this when a turn's task is
    # known to need only a few, so a derailed model has less surface to reach for.
    allowed: frozenset[str] | None = None
    # Host hook for CONFIRM: returns True if the operator approved. None means no
    # interactive channel is available, so anything needing confirmation is refused.
    ask: Callable[[str, dict, ToolPolicy], bool] | None = None

    refusals: int = 0

    # --- registration ----------------------------------------------------------------

    def register(self, spec: ToolSpec, run, policy: ToolPolicy) -> None:
        """Register a tool and declare its policy in one step.

        One call rather than two, because two calls can be half-made: a tool registered
        without a policy would be denied at run time (correct, but discovered late), and
        the reverse is a dangling declaration. `guard()` raises here if the name is
        structurally excluded — a wiring mistake becomes an import-time crash.
        """
        self.gate.book.guard(spec.name)
        if policy.name != spec.name:
            raise ValueError(f"policy names {policy.name!r} but tool is {spec.name!r}")
        self.gate.book.declare(policy)
        self.inner.register(spec, run)

    # --- the ToolBox shape the Runner uses -------------------------------------------

    def specs(self, only: set[str] | None = None) -> tuple[ToolSpec, ...]:
        """Only the tools this turn may use. Narrowing here rather than at execution
        means the model is never shown a tool it would be refused — cheaper, and it
        avoids teaching it to try."""
        names = set(self.inner.tools)
        if self.allowed is not None:
            names &= set(self.allowed)
        if only is not None:
            names &= set(only)
        return self.inner.specs(names)

    def execute(self, call: ToolCall) -> ToolResult:
        started = time.monotonic()

        # 1. Intent first. If the sink is broken this raises, and nothing runs.
        self.audit.intent(call.name, call.args)

        # 2. Per-turn allow-list.
        if self.allowed is not None and call.name not in self.allowed:
            return self._refuse(call, "not available for this task")

        # 3. Policy.
        outcome = self.gate.decide(call.name, call.args)

        if outcome.decision is Decision.CONFIRM:
            outcome = self._confirm(call, outcome.policy)

        # 4. Decision, whatever it turned out to be.
        self.audit.decision(call.name, str(outcome.decision), outcome.reason)

        if outcome.decision is not Decision.ALLOW:
            return self._refuse(call, outcome.reason, audited=True)

        # 5. Run it.
        result = self.inner.execute(call)
        self.gate.note_spend(call.name)

        # 6. Result — size only, never content.
        self.audit.result(call.name, ok=not result.is_error, size=len(result.content),
                          elapsed_ms=int((time.monotonic() - started) * 1000))
        return result

    # --- helpers -----------------------------------------------------------------------

    def _confirm(self, call: ToolCall, policy: ToolPolicy | None):
        """Ask the operator. Every path that is not an explicit approval is a refusal."""
        from agentkit.permissions import Outcome

        if policy is not None and policy.permission is Permission.ADMIN \
                and not self.context.may_confirm_admin():
            return Outcome(Decision.DENY,
                           f"admin actions cannot be confirmed from "
                           f"{self.context.surface}", policy)

        if self.ask is None:
            return Outcome(Decision.DENY,
                           "requires approval and no confirmation channel is available",
                           policy)

        pending = self.gate.request(call.name, call.args)
        try:
            approved = bool(self.ask(call.name, call.args, policy))
        except Exception as exc:  # noqa: BLE001 — a broken prompt must not mean "yes"
            return Outcome(Decision.DENY, f"confirmation failed: {type(exc).__name__}",
                           policy)
        if not approved:
            return Outcome(Decision.DENY, "declined by the operator", policy)

        # Redeem through the gatekeeper rather than trusting the boolean: this is what
        # binds the approval to these exact arguments and makes it single-use.
        return self.gate.redeem(pending.nonce, call.name, call.args)

    def _refuse(self, call: ToolCall, reason: str, *, audited: bool = False) -> ToolResult:
        self.refusals += 1
        if not audited:
            self.audit.decision(call.name, str(Decision.DENY), reason)
        self.audit.result(call.name, ok=False, size=0)
        # Phrased for the model: state the refusal, do not suggest a way around it.
        return ToolResult(call.id, call.name,
                          f"Refused: {reason}. Do not retry this call; "
                          f"continue with the tools you do have.",
                          is_error=True)
