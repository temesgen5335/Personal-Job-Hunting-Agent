"""What a unit of work needs from a model, and what came back.

A `TaskSpec` is declared next to its caller, never in a central registry — that is what
keeps the harness reusable outside any one application. It states requirements
(capability, steps, context) and escape hatches (a deterministic prefetch, subtasks, a
pure-Python fallback) so the router can serve the same task on a frontier model and on
a 7B one without the caller branching.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from agentkit.llm.capabilities import ModelCard, Tier
from agentkit.llm.types import ToolSpec, Usage


class Strategy(StrEnum):
    """How a task will be executed. `level` (below) is how degraded that is."""

    PLAIN = "plain"                          # one call, no tools, free text
    JSON_NATIVE = "json_native"              # provider-enforced JSON
    PROMPTED_JSON = "prompted_json"          # ask for JSON, parse tolerantly
    NATIVE_LOOP = "native_loop"              # full agentic loop
    RESTRICTED_LOOP = "restricted_loop"      # loop with trimmed tools + forced first call
    ACTION_CALL = "action_call"              # pick a tool + args; result is not synthesized
    PREFETCH_SINGLE_SHOT = "prefetch_single_shot"   # Python plans, model writes
    PROMPTED_TOOL_JSON = "prompted_tool_json"       # tool emulation for unproven backends
    DECOMPOSED = "decomposed"                # split into subtasks, stitch in Python
    DETERMINISTIC_FALLBACK = "deterministic_fallback"   # no model at all


# 0 = undegraded. Used to rank plans, so the best available execution wins.
DEGRADATION_LEVEL: dict[Strategy, int] = {
    Strategy.PLAIN: 0,
    Strategy.JSON_NATIVE: 0,
    Strategy.NATIVE_LOOP: 0,
    Strategy.ACTION_CALL: 0,
    Strategy.PROMPTED_JSON: 1,
    Strategy.RESTRICTED_LOOP: 1,
    Strategy.PREFETCH_SINGLE_SHOT: 2,
    Strategy.PROMPTED_TOOL_JSON: 2,
    Strategy.DECOMPOSED: 2,
    Strategy.DETERMINISTIC_FALLBACK: 3,
}

# How many tool steps each tier can actually sustain. Measured where possible: WEAK is
# 0 because a 7-9B model can emit a call but not carry state across the result.
STEP_BUDGET: dict[Tier, int] = {
    Tier.UNKNOWN: 2,
    Tier.TINY: 0,
    Tier.WEAK: 2,
    Tier.STANDARD: 5,
    Tier.STRONG: 12,
}


@dataclass(frozen=True)
class Budget:
    """Hard ceilings. Mandatory rather than optional: an agent loop on a paid model
    spends silently, and wall-clock is the only bound that always applies."""

    max_attempts: int = 3
    max_tool_calls: int = 12
    wall_clock_s: float = 120.0


@dataclass(frozen=True)
class TaskSpec:
    name: str
    min_tier: Tier = Tier.WEAK
    needs_tools: bool = False
    max_tool_steps: int = 0
    needs_json: bool = False
    # Whether the model must fold tool RESULTS into its answer. False means the tool
    # call IS the outcome (e.g. "dismiss this posting"), which a model that cannot run
    # a loop can still do perfectly well — the distinction the 8B measurement exposed.
    needs_synthesis: bool = True
    est_input_tokens: int = 0
    tools: tuple[ToolSpec, ...] = ()
    required_tools: frozenset[str] = frozenset()   # never trimmed when restricting
    # Deterministic context builder: runs the plan in Python so a weak model only has
    # to write the final answer. The single highest-value degradation path.
    prefetch: Callable[..., str] | None = None
    subtasks: tuple[TaskSpec, ...] = ()
    fallback: Callable[..., object] | None = None
    prefers_strong: bool = False     # rank by capability instead of chain order
    allow_degraded: bool = True
    budget: Budget = field(default_factory=Budget)


@dataclass(frozen=True)
class Plan:
    """A concrete way to run a task: this backend, this strategy."""

    backend: object            # anything with .name/.model/.card/.chat
    strategy: Strategy
    chain_index: int

    @property
    def level(self) -> int:
        return DEGRADATION_LEVEL[self.strategy]

    @property
    def card(self) -> ModelCard:
        return self.backend.card

    def __repr__(self) -> str:      # readable in failure messages
        return f"<Plan {self.backend.name}/{self.backend.model} {self.strategy} L{self.level}>"


@dataclass(frozen=True)
class Rejection:
    """Why a backend could not serve this task. Kept so the error message can tell the
    user what to change instead of just saying no."""

    backend_name: str
    model: str
    reason: str
    detail: str = ""

    def __str__(self) -> str:
        s = f"{self.backend_name}/{self.model}: {self.reason}"
        return f"{s} ({self.detail})" if self.detail else s


@dataclass(frozen=True)
class TaskOutcome:
    value: object
    provider: str
    model: str
    tier: Tier
    strategy: Strategy
    attempts: int = 1
    usage: Usage | None = None
    elapsed_ms: int = 0
    warnings: tuple[str, ...] = ()

    @property
    def degraded(self) -> bool:
        return DEGRADATION_LEVEL[self.strategy] > 0


class NoCapableModel(RuntimeError):
    """Nothing available can run this task. Carries the per-backend reasons."""

    def __init__(self, task: TaskSpec, rejections: list[Rejection]):
        self.task = task
        self.rejections = rejections
        need = [f"tier>={task.min_tier.name.lower()}"]
        if task.needs_tools:
            need.append(f"tools (<={task.max_tool_steps} steps)")
        if task.needs_json:
            need.append("json")
        lines = "\n  ".join(str(r) for r in rejections) or "(no backends configured)"
        super().__init__(f"task {task.name!r} needs {' + '.join(need)}; "
                         f"no backend qualified:\n  {lines}")
