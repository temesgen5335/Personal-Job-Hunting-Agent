"""Routing, degradation and the circuit breaker.

`choose_strategy` is pure, so the whole decision matrix is testable offline with no
network — which is the point of making it pure. The breaker uses an injected clock, so
these run instantly rather than timing real sleeps.
"""

import pytest

from agentkit.llm.capabilities import ModelCard, Tier
from agentkit.llm.errors import Classified, Verdict
from agentkit.llm.health import Breaker
from agentkit.llm.router import choose_strategy, describe, plans_for
from agentkit.llm.tasks import Strategy, TaskSpec
from agentkit.llm.types import ToolSpec

TOOL = ToolSpec("search", "Search things.",
                {"type": "object", "properties": {}, "required": []})


def card(tier, **kw):
    defaults = dict(model="m", tier=tier, context_tokens=128000)
    defaults.update(kw)
    return ModelCard(**defaults)


class FakeBackend:
    def __init__(self, name, card, model="m"):
        self.name, self.card, self.model = name, card, model
    def __repr__(self):
        return f"<{self.name}>"


# --- no-tool tasks -----------------------------------------------------------------

def test_plain_text_runs_on_anything_above_tiny():
    t = TaskSpec("summarize")
    assert choose_strategy(t, card(Tier.STRONG)) is Strategy.PLAIN
    assert choose_strategy(t, card(Tier.WEAK)) is Strategy.PLAIN
    assert choose_strategy(t, card(Tier.TINY)) is Strategy.PLAIN


def test_json_uses_native_mode_when_the_provider_has_it():
    t = TaskSpec("extract", needs_json=True)
    assert choose_strategy(t, card(Tier.STANDARD, json_object=True)) is Strategy.JSON_NATIVE
    assert choose_strategy(t, card(Tier.STANDARD, json_object=False)) is Strategy.PROMPTED_JSON


def test_wide_json_on_a_weak_model_decomposes_when_it_can():
    sub = (TaskSpec("part1"), TaskSpec("part2"))
    t = TaskSpec("wide", needs_json=True, subtasks=sub)
    assert choose_strategy(t, card(Tier.WEAK, json_object=False)) is Strategy.DECOMPOSED


def test_structured_output_is_not_offered_on_tiny():
    assert choose_strategy(TaskSpec("x", needs_json=True), card(Tier.TINY)) is None


# --- the measured 8B case ----------------------------------------------------------

def test_a_model_that_emits_calls_but_cannot_loop_gets_action_call():
    """llama-3.1-8b measured: emit 5/5, select 5/5, use-result 0/5. Dismissing a job is
    a tool call whose RESULT nobody needs — it can still do that perfectly."""
    weak_8b = card(Tier.WEAK, native_tools=True, tool_loop=False)
    action = TaskSpec("dismiss", needs_tools=True, max_tool_steps=1,
                      needs_synthesis=False, tools=(TOOL,))
    assert choose_strategy(action, weak_8b) is Strategy.ACTION_CALL


def test_the_same_model_cannot_take_a_task_that_needs_the_result():
    weak_8b = card(Tier.WEAK, native_tools=True, tool_loop=False)
    lookup = TaskSpec("answer", needs_tools=True, max_tool_steps=2, tools=(TOOL,))
    assert choose_strategy(lookup, weak_8b) is None      # no prefetch offered → cannot


def test_a_prefetch_rescues_exactly_that_case():
    """Python runs the plan, the model only writes the answer. The highest-value
    degradation, and the one that generalizes a pattern already in this codebase."""
    weak_8b = card(Tier.WEAK, native_tools=True, tool_loop=False)
    lookup = TaskSpec("answer", needs_tools=True, max_tool_steps=2, tools=(TOOL,),
                      prefetch=lambda **kw: "context")
    assert choose_strategy(lookup, weak_8b) is Strategy.PREFETCH_SINGLE_SHOT


# --- loop-capable tiers ------------------------------------------------------------

def test_capable_models_get_the_full_loop():
    t = TaskSpec("research", needs_tools=True, max_tool_steps=4, tools=(TOOL,))
    assert choose_strategy(t, card(Tier.STANDARD, tool_loop=True)) is Strategy.NATIVE_LOOP
    assert choose_strategy(t, card(Tier.STRONG, tool_loop=True)) is Strategy.NATIVE_LOOP


def test_a_deep_loop_exceeds_what_standard_sustains():
    deep = TaskSpec("deep", needs_tools=True, max_tool_steps=9, tools=(TOOL,),
                    prefetch=lambda **kw: "ctx")
    assert choose_strategy(deep, card(Tier.STRONG, tool_loop=True)) is Strategy.NATIVE_LOOP
    # STANDARD sustains 5 — falls back rather than attempting nine steps.
    assert choose_strategy(deep, card(Tier.STANDARD, tool_loop=True)) is Strategy.PREFETCH_SINGLE_SHOT


def test_a_weak_but_loop_capable_model_gets_a_restricted_loop():
    t = TaskSpec("short", needs_tools=True, max_tool_steps=2, tools=(TOOL,))
    assert choose_strategy(t, card(Tier.WEAK, tool_loop=True)) is Strategy.RESTRICTED_LOOP


def test_unproven_tool_support_uses_prompted_emulation():
    """A custom Ollama endpoint: native support is unknown, so use a protocol that
    works either way rather than guessing."""
    unknown = card(Tier.UNKNOWN, native_tools=None)
    t = TaskSpec("x", needs_tools=True, max_tool_steps=2, tools=(TOOL,))
    assert choose_strategy(t, unknown) is Strategy.PROMPTED_TOOL_JSON


def test_unknown_tier_is_treated_as_weak_never_as_capable():
    t = TaskSpec("research", needs_tools=True, max_tool_steps=4, tools=(TOOL,))
    unknown = card(Tier.UNKNOWN, native_tools=True, tool_loop=True)
    assert choose_strategy(t, unknown) is not Strategy.NATIVE_LOOP


# --- admission and ranking ---------------------------------------------------------

def test_the_plan_queue_never_includes_an_incapable_backend():
    """The structural guarantee: failover walks this queue, so it cannot land on a
    model that cannot do the task."""
    good = FakeBackend("groq", card(Tier.STANDARD, tool_loop=True))
    bad = FakeBackend("tiny", card(Tier.TINY))
    t = TaskSpec("research", needs_tools=True, max_tool_steps=3, tools=(TOOL,))
    plans, rejections = plans_for(t, [bad, good])
    assert [p.backend.name for p in plans] == ["groq"]
    assert any("min_tier" in r.reason for r in rejections)


def test_least_degraded_wins_over_chain_order():
    """A configured primary that can only degrade loses to a capable backend behind
    it — and the rejection explains why."""
    primary = FakeBackend("primary", card(Tier.WEAK, native_tools=True, tool_loop=False))
    secondary = FakeBackend("secondary", card(Tier.STANDARD, tool_loop=True))
    t = TaskSpec("research", needs_tools=True, max_tool_steps=2, tools=(TOOL,),
                 prefetch=lambda **kw: "ctx")
    plans, _ = plans_for(t, [primary, secondary])
    assert [p.backend.name for p in plans] == ["secondary", "primary"]
    assert plans[0].strategy is Strategy.NATIVE_LOOP
    assert plans[1].strategy is Strategy.PREFETCH_SINGLE_SHOT


def test_chain_order_still_breaks_ties():
    a = FakeBackend("a", card(Tier.STANDARD, tool_loop=True))
    b = FakeBackend("b", card(Tier.STANDARD, tool_loop=True))
    t = TaskSpec("x", needs_tools=True, max_tool_steps=2, tools=(TOOL,))
    plans, _ = plans_for(t, [a, b])
    assert [p.backend.name for p in plans] == ["a", "b"]


def test_prefers_strong_reorders_equally_undegraded_plans():
    """The assistant asks for the best available; the pipeline keeps chain order."""
    weak_ok = FakeBackend("first", card(Tier.STANDARD, tool_loop=True))
    strong = FakeBackend("second", card(Tier.STRONG, tool_loop=True))
    t = TaskSpec("x", needs_tools=True, max_tool_steps=2, tools=(TOOL,))
    assert [p.backend.name for p in plans_for(t, [weak_ok, strong])[0]] == ["first", "second"]
    t2 = TaskSpec("x", needs_tools=True, max_tool_steps=2, tools=(TOOL,), prefers_strong=True)
    assert [p.backend.name for p in plans_for(t2, [weak_ok, strong])[0]] == ["second", "first"]


def test_a_prompt_too_long_for_the_context_is_rejected_not_degraded():
    small = FakeBackend("small", card(Tier.STANDARD, tool_loop=True, context_tokens=8000))
    t = TaskSpec("big", est_input_tokens=50000)
    plans, rejections = plans_for(t, [small])
    assert plans == []
    assert any("context" in r.reason for r in rejections)


def test_a_task_may_forbid_degradation():
    weak = FakeBackend("weak", card(Tier.WEAK, native_tools=True, tool_loop=False))
    t = TaskSpec("strict", needs_tools=True, max_tool_steps=2, tools=(TOOL,),
                 prefetch=lambda **kw: "ctx", allow_degraded=False)
    plans, rejections = plans_for(t, [weak])
    assert plans == [] and any("degrading" in r.reason for r in rejections)


def test_rejections_tell_the_user_what_to_change():
    unknown = FakeBackend("custom", card(Tier.UNKNOWN), model="local-thing")
    t = TaskSpec("x", min_tier=Tier.STANDARD)
    _, rejections = plans_for(t, [unknown])
    assert "LLM_TIER_OVERRIDES" in rejections[0].detail

    no_loop = FakeBackend("groq", card(Tier.WEAK, native_tools=True, tool_loop=False))
    _, rej2 = plans_for(TaskSpec("y", needs_tools=True, max_tool_steps=3, tools=(TOOL,)),
                        [no_loop])
    assert "prefetch" in rej2[0].detail


def test_describe_explains_the_whole_decision():
    good = FakeBackend("groq", card(Tier.STANDARD, tool_loop=True))
    bad = FakeBackend("tiny", card(Tier.TINY))
    text = describe(TaskSpec("x", needs_tools=True, max_tool_steps=2, tools=(TOOL,)),
                    [good, bad])
    assert "eligible" in text and "rejected" in text and "native_loop" in text


# --- circuit breaker ---------------------------------------------------------------

class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


def _breaker():
    clock = Clock()
    return Breaker(now=clock, rng=lambda: 1.0), clock       # rng=1.0 → full cooldown


def test_a_dead_key_stops_being_retried_at_all():
    """The concrete fix: today a 401 costs a round trip on every single call."""
    b, _ = _breaker()
    b.record_failure("groq", "m", Classified(Verdict.PERMANENT, "bad key", 401))
    assert b.is_open("groq", "m")
    assert b.opens_in("groq", "m") == float("inf")


def test_a_config_reload_gives_a_fixed_key_a_fresh_chance():
    b, _ = _breaker()
    b.record_failure("groq", "m", Classified(Verdict.PERMANENT, "bad key", 401))
    b.reset()                                    # host calls this on settings reload
    assert not b.is_open("groq", "m")


def test_rate_limit_honors_the_server_s_own_retry_after():
    b, clock = _breaker()
    b.record_failure("groq", "m", Classified(Verdict.RATE_LIMIT, "slow", 429, retry_after_s=30))
    assert b.is_open("groq", "m")
    clock.advance(31)
    assert not b.is_open("groq", "m")


def test_one_blip_does_not_open_the_breaker():
    """Opening on a single 500 would disable a healthy provider."""
    b, _ = _breaker()
    for _ in range(2):
        b.record_failure("groq", "m", Classified(Verdict.TRANSIENT, "boom", 500))
    assert not b.is_open("groq", "m")
    b.record_failure("groq", "m", Classified(Verdict.TRANSIENT, "boom", 500))
    assert b.is_open("groq", "m")


def test_success_closes_the_breaker_and_resets_escalation():
    b, clock = _breaker()
    for _ in range(3):
        b.record_failure("groq", "m", Classified(Verdict.TRANSIENT, "boom", 500))
    clock.advance(61)
    b.record_success("groq", "m")
    for _ in range(2):
        b.record_failure("groq", "m", Classified(Verdict.TRANSIENT, "boom", 500))
    assert not b.is_open("groq", "m")            # counter restarted from zero


def test_repeated_outages_back_off_further_each_time():
    b, clock = _breaker()
    for _ in range(3):
        b.record_failure("groq", "m", Classified(Verdict.TRANSIENT, "boom", 500))
    first = b.opens_in("groq", "m")
    clock.advance(first + 1)
    for _ in range(3):
        b.record_failure("groq", "m", Classified(Verdict.TRANSIENT, "boom", 500))
    assert b.opens_in("groq", "m") > first


@pytest.mark.parametrize("verdict", [Verdict.BAD_REQUEST, Verdict.CAPABILITY,
                                     Verdict.CONTEXT, Verdict.CONTENT_FILTER])
def test_request_level_failures_never_disable_the_backend(verdict):
    """These are about this prompt, not the provider's health. Opening on them would
    take out a working backend because of one malformed request."""
    b, _ = _breaker()
    for _ in range(5):
        b.record_failure("groq", "m", Classified(verdict, "nope", 400))
    assert not b.is_open("groq", "m")


def test_breaker_state_is_per_provider_and_model():
    b, _ = _breaker()
    b.record_failure("groq", "a", Classified(Verdict.PERMANENT, "bad key", 401))
    assert b.is_open("groq", "a") and not b.is_open("groq", "b")


def test_an_open_backend_is_kept_out_of_the_plan_queue():
    b, _ = _breaker()
    dead = FakeBackend("dead", card(Tier.STANDARD, tool_loop=True))
    live = FakeBackend("live", card(Tier.STANDARD, tool_loop=True))
    b.record_failure("dead", "m", Classified(Verdict.PERMANENT, "bad key", 401))
    plans, rejections = plans_for(TaskSpec("x"), [dead, live], breaker=b)
    assert [p.backend.name for p in plans] == ["live"]
    assert any("cooling down" in r.reason for r in rejections)


def test_snapshot_reports_open_backends_for_diagnostics():
    b, _ = _breaker()
    b.record_failure("groq", "m", Classified(Verdict.PERMANENT, "bad key", 401))
    snap = b.snapshot()
    assert "groq/m" in snap and "permanently" in snap["groq/m"]


def test_unproven_tool_support_rejection_names_the_two_ways_out():
    """Gemini's OpenAI-compat tool support could not be confirmed (free quota
    exhausted during the spike), so its card carries tool_loop=None. The message must
    distinguish 'unproven' from 'incapable' and say what to do about it."""
    unproven = FakeBackend("gemini", card(Tier.STANDARD, native_tools=None, tool_loop=None))
    t = TaskSpec("deep", needs_tools=True, max_tool_steps=4, tools=(TOOL,))
    _, rejections = plans_for(t, [unproven])
    detail = rejections[0].detail
    assert "unproven" in detail
    assert "prefetch()" in detail and "registry" in detail
