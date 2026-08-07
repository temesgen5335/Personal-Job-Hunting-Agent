"""Pick how — and whether — a task can run on the models available.

Two pure functions, so the whole matrix is testable offline with no network (which the
project's test rules require):

  choose_strategy(task, card) -> Strategy | None    what this model could do
  plans_for(task, backends)   -> (plans, rejections) ranked queue + why the rest lost

The returned plan list IS the failover queue. That is the structural guarantee that
failover can never land on a model incapable of the task: an incapable backend never
enters the queue in the first place.

One consequence worth documenting where users will see it: the configured "primary"
provider no longer decides admission, it only breaks ties. A primary that cannot run a
task is skipped, and the rejection says so.
"""

from __future__ import annotations

from agentkit.llm.capabilities import ModelCard, Tier
from agentkit.llm.tasks import (
    DEGRADATION_LEVEL,
    STEP_BUDGET,
    Plan,
    Rejection,
    Strategy,
    TaskSpec,
)

# Prompts run long; leave headroom rather than discovering the limit mid-loop.
_CONTEXT_HEADROOM = 0.8


def choose_strategy(task: TaskSpec, card: ModelCard) -> Strategy | None:
    """The best execution this model can manage for this task, or None if it cannot.

    Pure: no I/O, no clock, no randomness — one parametrized test per cell.
    """
    tier = card.tier
    # UNKNOWN is admitted at WEAK's capability, never assumed better. A local model of
    # unknown provenance gets the cautious path until someone declares its tier.
    effective = Tier.WEAK if tier is Tier.UNKNOWN else tier

    if not task.needs_tools:
        if task.needs_json:
            if card.json_object:
                return Strategy.JSON_NATIVE
            if effective >= Tier.WEAK:
                return Strategy.DECOMPOSED if task.subtasks else Strategy.PROMPTED_JSON
            return None                       # TINY + structured output: not offered
        return Strategy.PLAIN if effective >= Tier.TINY else None

    # --- tools required ------------------------------------------------------------
    # A task whose tool CALL is the outcome does not need the model to carry state
    # across a result, so a model that can emit calls but not loop still qualifies.
    # This is exactly the llama-3.1-8b case: emit 5/5, select 5/5, use-result 0/5.
    if not task.needs_synthesis and task.max_tool_steps <= 1 and card.native_tools:
        return Strategy.ACTION_CALL

    budget = STEP_BUDGET[tier]

    if card.tool_loop and effective >= Tier.STANDARD and task.max_tool_steps <= budget:
        return Strategy.NATIVE_LOOP
    if card.tool_loop and effective >= Tier.WEAK and task.max_tool_steps <= 2:
        return Strategy.RESTRICTED_LOOP

    # Cannot (or unproven to) loop. Move the planning into Python and leave the model
    # only the write-up — the highest-value degradation and the lowest risk.
    if task.prefetch is not None and effective >= Tier.WEAK:
        return Strategy.PREFETCH_SINGLE_SHOT

    # Native tool support unproven (custom endpoints): emulate the protocol in prompts.
    if card.native_tools is None and effective >= Tier.WEAK and task.max_tool_steps <= 2:
        return Strategy.PROMPTED_TOOL_JSON

    if task.subtasks:
        return Strategy.DECOMPOSED
    return None


def plans_for(task: TaskSpec, backends, breaker=None) -> tuple[list[Plan], list[Rejection]]:
    """Rank the ways this task could run. Returns (queue, rejections).

    Admission filters; chain order only ranks. Ordering: least degraded first, then
    capability if the task prefers a strong model, then the configured chain order.
    """
    plans: list[Plan] = []
    rejections: list[Rejection] = []

    for index, backend in enumerate(backends):
        name, model, card = backend.name, backend.model, backend.card

        if breaker is not None and breaker.is_open(name, model):
            rejections.append(Rejection(name, model, "cooling down",
                                        breaker.reason(name, model)))
            continue

        if card.tier is not Tier.UNKNOWN and card.tier < task.min_tier:
            rejections.append(Rejection(name, model, "below min_tier",
                                        f"{card.tier.name.lower()} < {task.min_tier.name.lower()}"))
            continue

        if card.tier is Tier.UNKNOWN and task.min_tier > Tier.WEAK:
            rejections.append(Rejection(
                name, model, "tier unknown",
                f"set LLM_TIER_OVERRIDES={name}:{model}=<tier>"))
            continue

        # No strategy rescues a prompt that does not fit; say so plainly.
        if card.context_tokens and task.est_input_tokens > _CONTEXT_HEADROOM * card.context_tokens:
            rejections.append(Rejection(name, model, "context too small",
                                        f"needs ~{task.est_input_tokens} of {card.context_tokens}"))
            continue

        strategy = choose_strategy(task, card)
        if strategy is None:
            rejections.append(Rejection(name, model, "no viable strategy",
                                        _why_no_strategy(task, card)))
            continue

        if DEGRADATION_LEVEL[strategy] > 0 and not task.allow_degraded:
            rejections.append(Rejection(name, model, "would require degrading",
                                        f"{strategy} and the task forbids it"))
            continue

        plans.append(Plan(backend=backend, strategy=strategy, chain_index=index))

    plans.sort(key=lambda p: (
        p.level,
        -int(p.card.tier) if task.prefers_strong else 0,
        p.chain_index,
    ))
    return plans, rejections


def _why_no_strategy(task: TaskSpec, card: ModelCard) -> str:
    """A rejection the user can act on, not just a refusal."""
    if task.needs_tools:
        if card.tool_loop is False:
            return "emits tool calls but cannot use tool results; give the task a prefetch()"
        if card.native_tools is False:
            return "no tool support; give the task a prefetch() or subtasks"
        if card.tool_loop is None:
            # The tri-state paying off: unproven is not the same as incapable, and the
            # fix is either to prove it or to hand the task a deterministic path.
            return (f"tool-loop support unproven for this model; prompted emulation caps at "
                    f"2 steps (task needs {task.max_tool_steps}) — add a prefetch() or "
                    f"confirm the model and record tool_loop in the registry")
        if task.max_tool_steps > STEP_BUDGET[card.tier]:
            return f"needs {task.max_tool_steps} steps, tier sustains {STEP_BUDGET[card.tier]}"
    if task.needs_json and card.tier <= Tier.TINY:
        return "tier too low for structured output"
    return "capability requirements unmet"


def describe(task: TaskSpec, backends, breaker=None) -> str:
    """Human-readable routing decision — the body of a diagnostic command. Explaining
    why a chosen primary was skipped is the difference between a bug report and an
    understood behavior."""
    plans, rejections = plans_for(task, backends, breaker)
    lines = [f"task {task.name!r} (min_tier={task.min_tier.name.lower()}, "
             f"tools={task.needs_tools}, steps={task.max_tool_steps})"]
    if plans:
        lines.append("  eligible, best first:")
        lines += [f"    {p.backend.name}/{p.backend.model:<28} {p.strategy} (level {p.level})"
                  for p in plans]
    else:
        lines.append("  eligible: none")
    if rejections:
        lines.append("  rejected:")
        lines += [f"    {r}" for r in rejections]
    return "\n".join(lines)
