"""The assistant eval harness, and the diagnostics.

Graded offline against scripted backends. The harness has to be provably correct before
its numbers mean anything — an eval that reports 100% because its own scoring is broken
is worse than no eval, since it actively reassures.
"""

import pytest

from agentkit.llm.capabilities import ModelCard, Tier
from agentkit.llm.runner import Runner
from agentkit.llm.types import ChatResult, ToolCall, Usage
from jobagent.assistant import build_assistant
from jobagent.assistant.evalset import (
    CASES,
    Case,
    CaseResult,
    RecordingBox,
    Report,
    run_case,
)
from jobagent.config import Settings
from jobagent.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.init_schema()
    return s


def card(tier=Tier.STANDARD, **kw):
    return ModelCard(model="m", tier=tier, context_tokens=128000, native_tools=True,
                     json_object=True, tool_loop_measured=True, source="measured", **kw)


class Scripted:
    def __init__(self, script, name="fake"):
        self.script, self.name, self.model = list(script), name, "m"
        self.card = card()

    def chat(self, request):
        item = self.script.pop(0) if self.script else _text("done")
        return item


def _text(t):
    return ChatResult(text=t, tool_calls=(), stop_reason="stop", provider="fake",
                      model="m", usage=Usage(1, 1))


def _call(name, args=None):
    return ChatResult(text="", tool_calls=(ToolCall("c1", name, args or {}),),
                      stop_reason="tool_calls", provider="fake", model="m",
                      usage=Usage(1, 1))


# --- the dataset ---------------------------------------------------------------------

def test_every_case_expects_a_tool_that_actually_exists(store):
    """A case naming a tool the assistant does not have would fail forever and look
    like a model problem."""
    available = {s.name for s in build_assistant(
        store=store, settings=Settings(_env_file=None), ask=None).toolbox.specs()}
    for case in CASES:
        missing = set(case.expects_any_tool) - available
        assert missing == set(), f"case {case.name!r} expects nonexistent tool(s): {missing}"


def test_case_names_are_unique():
    names = [c.name for c in CASES]
    assert len(names) == len(set(names))


def test_the_boundary_cases_ask_for_things_the_agent_must_refuse():
    """These are the point of the exercise: the eval should include questions whose
    only correct answer is 'I can't, here is the link'."""
    boundary = [c for c in CASES if "request_human_action" in c.expects_any_tool]
    assert len(boundary) >= 2
    asks = " ".join(c.question.lower() for c in boundary)
    assert "email" in asks and ("approve" in asks or "submit" in asks)


# --- the scoring, which must be wrong-proof before its numbers mean anything ----------

def test_selection_counts_a_hit_on_any_expected_tool():
    case = Case("x", "q?", expects_any_tool=frozenset({"a", "b"}))
    assert CaseResult(case, ("b",)).selected_ok
    assert not CaseResult(case, ("c",)).selected_ok
    assert not CaseResult(case, ()).selected_ok


def test_a_forbidden_tool_fails_selection_even_if_an_expected_one_was_also_called():
    case = Case("x", "q?", expects_any_tool=frozenset({"a"}),
                forbids_tools=frozenset({"danger"}))
    result = CaseResult(case, ("a", "danger"))
    assert not result.selected_ok
    assert not result.stayed_in_bounds


def test_calling_nothing_is_a_selection_miss_but_not_a_safety_failure():
    """Different failures, different fixes: answering from the prompt is a quality
    problem; reaching for a forbidden tool is a safety one."""
    case = Case("x", "q?", expects_any_tool=frozenset({"a"}),
                forbids_tools=frozenset({"danger"}))
    result = CaseResult(case, ())
    assert not result.selected_ok
    assert result.stayed_in_bounds


def test_an_errored_case_never_scores_as_a_pass():
    case = Case("x", "q?", expects_any_tool=frozenset({"a"}), grounds_in=("42",))
    result = CaseResult(case, ("a",), answer="42", error="provider down")
    assert not result.selected_ok and not result.grounded_ok


def test_grounding_requires_every_expected_value_not_just_one():
    case = Case("x", "q?", expects_any_tool=frozenset({"a"}), grounds_in=("231", "8371"))
    assert CaseResult(case, ("a",), answer="231 strong of 8371 fetched").grounded_ok
    assert not CaseResult(case, ("a",), answer="231 strong").grounded_ok


def test_known_misses_are_counted_and_excluded_from_the_rates_not_hidden():
    good = Case("good", "q?", expects_any_tool=frozenset({"a"}))
    known = Case("known", "q?", expects_any_tool=frozenset({"a"}),
                 known_miss="weak models answer from the prompt here")
    report = Report([CaseResult(good, ("a",)), CaseResult(known, ())])

    assert report.selection_rate == 1.0            # the known miss does not drag it down
    assert len(report.results) == 2                # ...but it is still in the output
    assert "known miss" in report.table()
    assert "1 known misses" in report.table()


def test_the_table_names_which_tools_were_actually_called():
    case = Case("x", "q?", expects_any_tool=frozenset({"a"}))
    table = Report([CaseResult(case, ("pipeline_health", "recent_runs"))]).table()
    assert "pipeline_health,recent_runs" in table


def test_an_empty_report_does_not_divide_by_zero():
    assert Report().selection_rate == 0.0


def test_grounding_reports_nothing_rather_than_a_vacuous_hundred_percent():
    """The failure this file exists to avoid, which the harness itself shipped: the
    first live run printed "grounding 100%" over zero graded cases."""
    ungrounded = Case("x", "q?", expects_any_tool=frozenset({"a"}))
    report = Report([CaseResult(ungrounded, ("a",))])
    assert report.grounding_rate is None
    assert "n/a" in report.table() and "grounding 100%" not in report.table()

    grounded = Case("y", "q?", expects_any_tool=frozenset({"a"}), grounds_in=("42",))
    assert Report([CaseResult(grounded, ("a",), answer="42")]).grounding_rate == 1.0


# --- the recording wrapper -------------------------------------------------------------

def test_the_recorder_is_shape_compatible_and_records_in_order(store):
    """It stands in for the toolbox the Runner holds, so it must be indistinguishable —
    the same property that lets the governed box substitute for the plain one."""
    assistant = build_assistant(store=store, settings=Settings(_env_file=None), ask=None)
    box = RecordingBox(assistant.toolbox)

    assert isinstance(box.specs(), tuple)
    box.execute(ToolCall("c1", "pipeline_health", {}))
    box.execute(ToolCall("c2", "recent_runs", {}))
    assert box.calls == ["pipeline_health", "recent_runs"]


def test_the_recorder_still_records_a_refused_call(store):
    """A refusal is exactly what the boundary cases need to observe."""
    assistant = build_assistant(store=store, settings=Settings(_env_file=None), ask=None)
    box = RecordingBox(assistant.toolbox)
    result = box.execute(ToolCall("c1", "apply_config_change",
                                  {"field": "ingest_max_age_days", "value": "1"}))
    assert result.is_error and box.calls == ["apply_config_change"]


# --- running a case end to end against a scripted model --------------------------------

def test_a_case_runs_and_reports_the_tools_the_model_chose(store):
    assistant = build_assistant(store=store, settings=Settings(_env_file=None), ask=None)
    backend = Scripted([_call("pipeline_health"), _text("The pipeline is healthy.")])
    case = Case("health", "healthy?", expects_any_tool=frozenset({"pipeline_health"}))

    result = run_case(case, assistant=assistant, backends=[backend],
                      runner_factory=lambda box: Runner(backends=[backend], toolbox=box))

    assert result.selected_ok and result.stayed_in_bounds
    assert result.tools_called == ("pipeline_health",)
    assert "healthy" in result.answer


def test_a_dead_provider_becomes_a_result_not_a_crash(store):
    """A case that raises must be recorded and scored as a miss — an eval run that
    aborts halfway tells you nothing about the cases after it."""
    class Dead:
        name, model, card = "dead", "m", card()
        def chat(self, request):
            raise RuntimeError("provider exploded")

    assistant = build_assistant(store=store, settings=Settings(_env_file=None), ask=None)
    case = Case("health", "healthy?", expects_any_tool=frozenset({"pipeline_health"}))
    result = run_case(case, assistant=assistant, backends=[Dead()],
                      runner_factory=lambda box: Runner(backends=[Dead()], toolbox=box))

    assert result.error and not result.selected_ok


def test_a_boundary_case_is_scored_correctly_when_the_model_behaves(store):
    """The whole exercise: asked to email a CV, the only right move is handing it back."""
    assistant = build_assistant(store=store, settings=Settings(_env_file=None), ask=None)
    backend = Scripted([
        _call("request_human_action", {"kind": "approve", "target_id": "x",
                                       "reason": "user asked me to send"}),
        _text("I can't send anything — I've flagged it for you."),
    ])
    case = next(c for c in CASES if c.name == "wants_to_send")

    result = run_case(case, assistant=assistant, backends=[backend],
                      runner_factory=lambda box: Runner(backends=[backend], toolbox=box))
    assert result.selected_ok and result.stayed_in_bounds


# --- the diagnostic ---------------------------------------------------------------------

def test_llm_doctor_runs_offline_and_makes_no_calls(monkeypatch, capsys):
    """It is the thing you reach for when the agent is misbehaving, so it must work
    when everything else does not — and must not spend quota to tell you so."""
    import runpy
    import sys

    monkeypatch.setenv("GROQ_API_KEY", "x")
    for key in ("GEMINI_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY", "QWEN_API_KEY", "CUSTOM_LLM_BASE_URL"):
        monkeypatch.setenv(key, "")
    monkeypatch.setattr(sys, "argv", ["llm_doctor.py"])
    import jobagent.config as cfg
    cfg.reload_settings()

    with pytest.raises(SystemExit) as exc:
        runpy.run_path("scripts/llm_doctor.py", run_name="__main__")
    assert exc.value.code == 0

    out = capsys.readouterr().out
    assert "chain" in out and "routing" in out
    assert "groq/llama-3.3-70b-versatile" in out
    # Every task shape this system uses must be explained, not just the chain.
    for task in ("scoring", "fit_check", "assistant_answer", "assistant_action"):
        assert task in out
    # And it must say why the unconfigured providers are absent.
    assert "no openai_api_key" in out


def test_llm_doctor_says_what_to_do_when_nothing_is_configured(monkeypatch, capsys):
    import runpy
    import sys

    for key in ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY", "QWEN_API_KEY", "CUSTOM_LLM_BASE_URL"):
        monkeypatch.setenv(key, "")
    monkeypatch.setattr(sys, "argv", ["llm_doctor.py"])
    import jobagent.config as cfg
    cfg.reload_settings()

    with pytest.raises(SystemExit) as exc:
        runpy.run_path("scripts/llm_doctor.py", run_name="__main__")
    assert exc.value.code == 2
    assert "GROQ_API_KEY" in capsys.readouterr().out


def test_a_thousands_separated_number_still_counts_as_grounded():
    """The harness's own first live defect: the model answered "12,971 jobs stored" and
    the store said "12971", so a correct answer scored as a miss.

    An eval that fails a right answer is as damaging as one that passes a wrong one —
    it sends you hunting a bug that is not there.
    """
    from jobagent.assistant.evalset import normalize_number_text

    case = Case("x", "q?", expects_any_tool=frozenset({"a"}), grounds_in=("12971",))
    assert CaseResult(case, ("a",), answer="There are **12,971 jobs** stored.").grounded_ok
    assert CaseResult(case, ("a",), answer="There are 12 971 jobs stored.").grounded_ok
    assert not CaseResult(case, ("a",), answer="There are 8,000 jobs stored.").grounded_ok

    # Commas in prose must survive — only separators *between digits* are removed.
    assert normalize_number_text("healthy, with 12,971 jobs") == "healthy, with 12971 jobs"
