"""Strategy execution, tool dispatch, tolerant JSON, and the failover walk.

All offline against scripted fake backends. The point of routing before running is that
failover cannot land on an incapable model; the point of scripting the backends is that
each degradation path can be proven without spending quota to do it.
"""

import pytest

from agentkit.llm import jsonx
from agentkit.llm.capabilities import ModelCard, Tier
from agentkit.llm.health import Breaker
from agentkit.llm.runner import Runner
from agentkit.llm.strategies import BudgetExceeded, EXECUTORS
from agentkit.llm.tasks import Budget, NoCapableModel, Strategy, TaskSpec
from agentkit.llm.types import ChatResult, ToolCall, ToolSpec, Usage
from agentkit.tools import ToolBox

SEARCH = ToolSpec("search", "Search the store.",
                  {"type": "object",
                   "properties": {"q": {"type": "string", "description": "query"}},
                   "required": ["q"]})


def card(tier=Tier.STANDARD, **kw):
    if "tool_loop" in kw:
        kw["tool_loop_measured"] = kw.pop("tool_loop")
    d = dict(model="m", tier=tier, context_tokens=128000, native_tools=True,
             json_object=True, source="measured")
    d.update(kw)
    return ModelCard(**d)


class ScriptedBackend:
    """Replays a list of ChatResults (or raises, if the entry is an exception)."""

    def __init__(self, script, name="fake", model="m", card_=None):
        self.script = list(script)
        self.name, self.model = name, model
        self.card = card_ or card()
        self.requests = []

    def chat(self, request):
        self.requests.append(request)
        assert self.script, f"{self.name}: the model was called more times than scripted"
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _text(t, **kw):
    return ChatResult(text=t, tool_calls=(), stop_reason="stop",
                      provider="fake", model="m", usage=Usage(10, 5), **kw)


def _call(name="search", args=None, id="c1"):
    return ChatResult(text="", tool_calls=(ToolCall(id=id, name=name, args=args or {"q": "x"}),),
                      stop_reason="tool_calls", provider="fake", model="m",
                      usage=Usage(10, 5))


def toolbox(fn=None):
    tb = ToolBox()
    tb.register(SEARCH, fn or (lambda args: f"found: {args.get('q')}"))
    return tb


# --- jsonx: every rule here is a failure that was actually observed -----------------

def test_literal_newlines_inside_strings_parse():
    # The exact shape that once put a raw JSON blob into an outgoing email body.
    assert jsonx.loads_object('{"body": "line one\nline two"}')["body"].startswith("line one")


def test_fences_and_surrounding_prose_are_stripped():
    assert jsonx.loads_object('Here you go:\n```json\n{"a": 1}\n```\nHope that helps!') == {"a": 1}


def test_braces_inside_strings_do_not_break_extraction():
    assert jsonx.loads_object('junk {"a": "}"} junk') == {"a": "}"}


def test_trailing_commas_are_repaired():
    assert jsonx.loads_object('{"a": 1,}') == {"a": 1}


def test_null_and_arrays_are_not_objects():
    assert jsonx.loads_object("null") is None
    assert jsonx.loads_object("[1,2]") is None
    assert jsonx.loads("[1,2]") == [1, 2]


def test_hopeless_input_returns_none_rather_than_raising():
    assert jsonx.loads("not json at all") is None
    assert jsonx.loads("") is None


def test_missing_keys_names_what_a_repair_prompt_should_ask_for():
    assert jsonx.missing_keys({"a": 1}, ("a", "b", "c")) == ("b", "c")


# --- ToolBox ------------------------------------------------------------------------

def test_a_raising_tool_becomes_an_error_result_not_an_exception():
    def boom(args):
        raise RuntimeError("db is down")
    tb = toolbox(boom)
    res = tb.execute(ToolCall("c1", "search", {"q": "x"}))
    assert res.is_error and "db is down" in res.content


def test_an_invented_tool_name_is_refused_and_counted():
    tb = toolbox()
    res = tb.execute(ToolCall("c1", "delete_everything", {}))
    assert res.is_error and tb.unknown_calls == 1
    assert "search" in res.content          # tells the model what does exist


def test_unparseable_arguments_are_reported_back_to_the_model():
    tb = toolbox()
    res = tb.execute(ToolCall("c1", "search", {}, parse_error="invalid JSON arguments"))
    assert res.is_error and "invalid JSON" in res.content


def test_an_unportable_schema_is_rejected_at_registration():
    tb = ToolBox()
    with pytest.raises(ValueError, match="unportable"):
        tb.register(ToolSpec("bad", "x", {"type": "object",
                                          "properties": {"a": {"$ref": "#/x"}}}), lambda a: "")


def test_long_results_are_truncated_so_one_tool_cannot_eat_the_context():
    tb = ToolBox(max_result_chars=20)
    tb.register(SEARCH, lambda args: "y" * 500)
    assert len(tb.execute(ToolCall("c1", "search", {"q": "x"})).content) < 100


# --- strategies ----------------------------------------------------------------------

def test_native_loop_feeds_the_result_back_and_answers():
    b = ScriptedBackend([_call(), _text("I found x.")])
    task = TaskSpec("ask", needs_tools=True, max_tool_steps=5, tools=(SEARCH,))
    out = EXECUTORS[Strategy.NATIVE_LOOP](task, b, toolbox(), {"prompt": "find x"}, Budget())
    assert out == "I found x."
    # The tool result must reach the model, grouped in one role="tool" message.
    second = b.requests[1].messages
    assert second[-1].role == "tool" and "found: x" in second[-1].tool_results[0].content


def test_native_loop_stops_at_the_step_budget_and_still_answers():
    # A model that never stops calling tools must not spin forever.
    b = ScriptedBackend([_call(), _call(), _text("ok, here is what I have")])
    task = TaskSpec("ask", needs_tools=True, max_tool_steps=2, tools=(SEARCH,))
    out = EXECUTORS[Strategy.NATIVE_LOOP](task, b, toolbox(), {"prompt": "p"}, Budget())
    assert out == "ok, here is what I have"


def test_the_tool_call_budget_is_a_hard_stop():
    b = ScriptedBackend([_call(), _call(), _call(), _text("done")])
    task = TaskSpec("ask", needs_tools=True, max_tool_steps=9, tools=(SEARCH,))
    with pytest.raises(BudgetExceeded):
        EXECUTORS[Strategy.NATIVE_LOOP](task, b, toolbox(), {"prompt": "p"},
                                        Budget(max_tool_calls=2))


def test_action_call_returns_the_tool_result_without_a_second_model_call():
    # The whole point: a model that cannot use a tool RESULT can still do this task.
    b = ScriptedBackend([_call(args={"q": "dismiss"})])
    task = TaskSpec("act", needs_tools=True, max_tool_steps=1,
                    needs_synthesis=False, tools=(SEARCH,))
    out = EXECUTORS[Strategy.ACTION_CALL](task, b, toolbox(), {"prompt": "p"}, Budget())
    assert out == "found: dismiss"
    assert len(b.requests) == 1


def test_prefetch_single_shot_makes_exactly_one_call_with_python_gathered_context():
    b = ScriptedBackend([_text("Telegram is stale because the session expired.")])
    task = TaskSpec("why", needs_tools=True, max_tool_steps=4, tools=(SEARCH,),
                    prefetch=lambda inputs, toolbox: "LAST RUN: telegram error 'session expired'")
    out = EXECUTORS[Strategy.PREFETCH_SINGLE_SHOT](task, b, toolbox(), {"prompt": "why?"},
                                                   Budget())
    assert "session expired" in out
    assert len(b.requests) == 1
    assert "session expired" in b.requests[0].messages[0].text   # context reached the model


def test_restricted_loop_trims_tools_and_forces_the_first_call():
    tb = ToolBox()
    for i in range(6):
        tb.register(ToolSpec(f"t{i}", "d", {"type": "object", "properties": {}}),
                    lambda a, i=i: f"r{i}")
    b = ScriptedBackend([_call(name="t0", args={}), _text("answer")])
    task = TaskSpec("ask", needs_tools=True, max_tool_steps=2)
    out = EXECUTORS[Strategy.RESTRICTED_LOOP](task, b, tb, {"prompt": "p"}, Budget())
    assert out == "answer"
    assert len(b.requests[0].tools) <= 3
    assert b.requests[0].tool_choice == "required"
    # Python restates what was learned, so the model carries no state across the turn.
    assert "So far you have learned" in b.requests[1].messages[-1].text


def test_prompted_tool_json_emulates_the_protocol_for_unproven_backends():
    b = ScriptedBackend([_text('{"tool": "search", "args": {"q": "jobs"}}'),
                         _text('{"final": "there are 3 jobs"}')])
    task = TaskSpec("ask", needs_tools=True, max_tool_steps=2, tools=(SEARCH,))
    out = EXECUTORS[Strategy.PROMPTED_TOOL_JSON](task, b, toolbox(), {"prompt": "p"}, Budget())
    assert out == "there are 3 jobs"


def test_prompted_tool_json_takes_plain_prose_at_face_value():
    b = ScriptedBackend([_text("I think there are three.")])
    task = TaskSpec("ask", needs_tools=True, max_tool_steps=2, tools=(SEARCH,))
    out = EXECUTORS[Strategy.PROMPTED_TOOL_JSON](task, b, toolbox(), {"prompt": "p"}, Budget())
    assert out == "I think there are three."


def test_json_native_repairs_once_before_giving_up():
    b = ScriptedBackend([_text("sorry, no JSON for you"), _text('{"ok": true}')])
    task = TaskSpec("extract", needs_json=True)
    out = EXECUTORS[Strategy.JSON_NATIVE](task, b, toolbox(), {"prompt": "p"}, Budget())
    assert jsonx.loads_object(out) == {"ok": True}
    assert len(b.requests) == 2


# --- the Runner's failover walk -------------------------------------------------------

def _runner(backends, **kw):
    kw.setdefault("sleep", lambda s: None)
    return Runner(backends=backends, toolbox=toolbox(), breaker=Breaker(now=lambda: 0.0),
                  **kw)


class Boom(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status_code = status


def test_a_permanent_failure_moves_on_and_the_breaker_stays_shut():
    dead = ScriptedBackend([Boom("Incorrect API key", 401)], name="groq")
    good = ScriptedBackend([_text("hello")], name="gemini")
    r = _runner([dead, good])
    out = r.run(TaskSpec("say"), prompt="hi")

    assert out.value == "hello" and out.provider == "gemini"
    assert r.breaker.is_open("groq", "m")            # never retried this process
    assert len(dead.requests) == 1                    # and not retried in place


def test_a_transient_failure_retries_the_same_backend_first():
    flaky = ScriptedBackend([Boom("upstream hiccup", 503), _text("recovered")], name="groq")
    r = _runner([flaky])
    out = r.run(TaskSpec("say"), prompt="hi")
    assert out.value == "recovered" and out.attempts == 2


def test_a_long_rate_limit_switches_backend_instead_of_waiting():
    class Limited(Exception):
        status_code = 429
        class response:                     # noqa: D106 — mimics the SDK's shape
            headers = {"retry-after": "120"}

    slept = []
    limited = ScriptedBackend([Limited()], name="groq")
    other = ScriptedBackend([_text("from the backup")], name="gemini")
    r = _runner([limited, other], sleep=slept.append)
    out = r.run(TaskSpec("say"), prompt="hi")

    assert out.provider == "gemini"
    assert slept == []                       # 120s is a reason to switch, not to sit still


def test_failover_never_lands_on_a_model_that_cannot_do_the_task():
    weak = ScriptedBackend([_text("nope")], name="weak",
                           card_=card(Tier.WEAK, tool_loop=False))
    strong = ScriptedBackend([_call(), _text("done")], name="strong",
                             card_=card(Tier.STRONG, tool_loop=True))
    task = TaskSpec("ask", needs_tools=True, max_tool_steps=4, tools=(SEARCH,))
    out = _runner([weak, strong]).run(task, prompt="p")

    assert out.provider == "strong"
    assert weak.requests == []               # the weak model was never even called


def test_the_attempt_budget_bounds_the_whole_walk():
    # 500 is transient, so each backend is worth one in-place retry; the budget is what
    # stops the walk, not the number of backends.
    backends = [ScriptedBackend([Boom("down", 500)] * 2, name=f"b{i}") for i in range(5)]
    r = _runner(backends)
    with pytest.raises(NoCapableModel):
        r.run(TaskSpec("say", budget=Budget(max_attempts=2)), prompt="hi")
    assert sum(len(b.requests) for b in backends) == 2


def test_an_exhausted_queue_uses_the_task_fallback_and_says_so():
    dead = ScriptedBackend([Boom("nope", 401)], name="groq")
    task = TaskSpec("say", fallback=lambda inputs: "computed without a model")
    out = _runner([dead]).run(task, prompt="hi")

    assert out.value == "computed without a model"
    assert out.strategy is Strategy.DETERMINISTIC_FALLBACK and out.degraded
    assert any("deterministic fallback" in w for w in out.warnings)


def test_the_failure_message_reads_in_the_order_things_happened():
    # The message is the whole diagnostic: "budget spent" listed before the failures
    # that spent it reads as the cause rather than the consequence.
    backends = [ScriptedBackend([Boom("nope", 401)], name=f"b{i}") for i in range(3)]
    with pytest.raises(NoCapableModel) as exc:
        _runner(backends).run(TaskSpec("say", budget=Budget(max_attempts=2)), prompt="hi")

    text = str(exc.value)
    assert text.index("b0") < text.index("b1") < text.index("attempt budget spent")


def test_no_backend_at_all_raises_with_actionable_reasons():
    with pytest.raises(NoCapableModel, match="no backend qualified"):
        _runner([]).run(TaskSpec("say"), prompt="hi")


def test_degradation_is_reported_rather_than_hidden():
    weak = ScriptedBackend([_text("a rough answer")], name="local",
                           card_=card(Tier.WEAK, tool_loop=False))
    task = TaskSpec("ask", needs_tools=True, max_tool_steps=4, tools=(SEARCH,),
                    prefetch=lambda inputs, toolbox: "context")
    out = _runner([weak]).run(task, prompt="p")

    assert out.strategy is Strategy.PREFETCH_SINGLE_SHOT
    assert out.degraded and any("degraded" in w for w in out.warnings)


def test_usage_is_totalled_across_every_call_of_a_multi_step_run():
    b = ScriptedBackend([_call(), _text("done")])
    task = TaskSpec("ask", needs_tools=True, max_tool_steps=5, tools=(SEARCH,))
    out = _runner([b]).run(task, prompt="p")
    assert out.usage.input_tokens == 20 and out.usage.output_tokens == 10


def test_a_budget_stop_does_not_burn_the_rest_of_the_queue():
    # Too expensive on one model is too expensive on the next; failing over would just
    # spend the ceiling twice.
    greedy = ScriptedBackend([_call(), _call()], name="a")
    spare = ScriptedBackend([_text("unused")], name="b")
    task = TaskSpec("ask", needs_tools=True, max_tool_steps=5, tools=(SEARCH,),
                    budget=Budget(max_tool_calls=1))
    with pytest.raises(BudgetExceeded):
        _runner([greedy, spare]).run(task, prompt="p")
    assert spare.requests == []


def test_events_are_emitted_for_the_audit_trail():
    seen = []
    dead = ScriptedBackend([Boom("nope", 401)], name="groq")
    good = ScriptedBackend([_text("ok")], name="gemini")
    Runner(backends=[dead, good], toolbox=toolbox(), breaker=Breaker(now=lambda: 0.0),
           sleep=lambda s: None,
           on_event=lambda k, p: seen.append(k)).run(TaskSpec("say"), prompt="hi")
    assert "plans" in seen and "attempt_failed" in seen and "attempt_ok" in seen
