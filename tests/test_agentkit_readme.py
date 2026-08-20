"""Every code example in `src/agentkit/README.md` must actually run.

A README for a package meant to be lifted into other projects is an interface promise.
Writing it from memory produced two broken examples on the first pass — `c.retry_after`
(the field is `retry_after_s`) and a tool schema missing the per-property `description`
that `validate_tool_schema` requires. Both would have failed for the first person who
copied them, and neither is visible by reading.

These tests are the examples, executed. Nothing here makes a network call: the point is
the API surface, not the providers.
"""

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

README = Path(__file__).resolve().parent.parent / "src" / "agentkit" / "README.md"


def test_the_readme_exists_and_travels_with_the_package():
    """It lives inside the package on purpose — vendoring `src/agentkit/` must carry
    its own documentation, or the copy arrives undocumented."""
    assert README.exists()
    assert "agentkit" in README.read_text()[:200]


# --- §1-3: construction -------------------------------------------------------

def test_quickstart_builds_a_chain():
    from agentkit.llm.service import LLMService

    cfg = SimpleNamespace(groq_api_key="gsk_x", llm_provider="groq")
    assert LLMService.from_settings(cfg).chain == ["groq"]


def test_settings_can_be_a_namespace_built_from_the_environment():
    import os

    from agentkit.llm.service import LLMService

    cfg = SimpleNamespace(**{k.lower(): v for k, v in os.environ.items()})
    LLMService.from_settings(cfg)          # must not raise whatever the env holds


def test_a_local_openai_compatible_server_needs_no_key():
    from agentkit.llm.service import LLMService

    svc = LLMService.from_settings(SimpleNamespace(
        custom_llm_base_url="http://localhost:11434/v1", custom_llm_model="llama3.2"))
    assert "custom" in svc.chain


def test_a_host_can_bring_its_own_provider_table():
    from agentkit.llm.chain import ProviderSpec
    from agentkit.llm.service import LLMService

    spec = ProviderSpec("housebrand", "house_key", "house_model",
                        "https://llm.internal/v1", default_model="house-1")
    svc = LLMService.from_providers([spec], SimpleNamespace(house_key="k"),
                                    order=("housebrand",))
    assert svc.chain == ["housebrand"]


def test_skip_reasons_and_describe():
    from agentkit.llm.service import LLMService

    svc = LLMService.from_settings(SimpleNamespace())
    assert svc.chain == [] and dict(svc.skipped).get("groq")
    assert isinstance(svc.describe(), str)


def test_the_provider_table_in_the_readme_matches_the_code():
    """The table names key attributes people will put in their config. A stale row is
    a silent misconfiguration: the provider simply never joins the chain."""
    from agentkit.llm.chain import DEFAULT_PROVIDERS

    text = README.read_text()
    for spec in DEFAULT_PROVIDERS:
        assert f"`{spec.key_field}`" in text, f"README omits {spec.key_field}"
        assert f"`{spec.model_field}`" in text, f"README omits {spec.model_field}"


# --- §4-5: ledger and errors --------------------------------------------------

def test_ledger_surface():
    from agentkit.llm.errors import Verdict
    from agentkit.llm.ledger import Ledger

    led = Ledger()
    led.record("groq", "m", ok=True, latency_s=0.5)
    led.record("gemini", "m", ok=False, latency_s=0.2, verdict=Verdict.RATE_LIMIT,
               detail="429")
    assert led.working() == [("groq", "m")]
    assert led.broken() == [("gemini", "m")]
    assert led.as_dict()["backends"] and isinstance(led.render(), str)
    assert isinstance(led.events("groq"), list)


def test_classify_exposes_the_documented_fields():
    """First draft of the README said `c.retry_after`; the field is `retry_after_s`."""
    from agentkit.llm.errors import classify

    c = classify(RuntimeError("401 invalid api key"))
    for attr in ("verdict", "message", "status", "retry_after_s"):
        assert hasattr(c, attr), f"README documents c.{attr}, which does not exist"


def test_every_documented_verdict_exists():
    from agentkit.llm.errors import Verdict

    text = README.read_text()
    for verdict in Verdict:
        assert f"`{verdict.name}`" in text, f"README omits the {verdict.name} verdict"


# --- §6-7: tools and governance ----------------------------------------------

def _weather_box():
    from agentkit.llm.types import ToolSpec
    from agentkit.tools import ToolBox

    box = ToolBox()
    box.register(
        ToolSpec("get_weather", "Current weather for a city.",
                 {"type": "object",
                  "properties": {"city": {"type": "string",
                                          "description": "City name, e.g. Berlin"}},
                  "required": ["city"]}),
        lambda args: f"{args['city']}: 17C, cloudy",
    )
    return box


def test_the_documented_tool_registration_is_accepted():
    """The first draft omitted the per-property description and was rejected outright
    by validate_tool_schema — the example could never have worked."""
    assert _weather_box().specs()


def test_the_documented_task_and_runner_construction():
    from agentkit.llm.runner import Runner
    from agentkit.llm.tasks import Budget, TaskSpec

    box = _weather_box()
    TaskSpec(name="weather", needs_tools=True, max_tool_steps=4, tools=box.specs(),
             budget=Budget(max_attempts=3, max_tool_calls=12, wall_clock_s=120))
    Runner(backends=[], toolbox=box)


def test_the_governed_toolbox_example_drops_into_the_runner():
    """The claim in §7 is that GuardedToolBox has the same shape as ToolBox, so there
    is no ungoverned path. If that stopped being true, this stops constructing."""
    from agentkit.audit import Auditor
    from agentkit.guard import GuardedToolBox
    from agentkit.llm.runner import Runner
    from agentkit.permissions import (
        Confirm,
        Gatekeeper,
        Permission,
        PolicyBook,
        ToolPolicy,
    )
    from agentkit.session import SessionContext, Surface

    book = PolicyBook(
        policies={
            "search": ToolPolicy("search", Permission.READ, Confirm.NEVER),
            "send_email": ToolPolicy("send_email", Permission.ACT, Confirm.SESSION),
            "set_config": ToolPolicy("set_config", Permission.ADMIN, Confirm.ALWAYS,
                                     describes="Change a system setting"),
        },
        excluded=frozenset({"delete_everything"}),
        cost_budget=20,
    )
    guarded = GuardedToolBox(
        inner=_weather_box(), gate=Gatekeeper(book), audit=Auditor(sink=None),
        context=SessionContext(actor="operator", surface=Surface.CLI, run_id="run-1"),
        ask=lambda name, args, policy: True,
    )
    assert hasattr(guarded, "execute") and hasattr(guarded, "specs")
    Runner(backends=[], toolbox=guarded)


# --- §8: retrieval ------------------------------------------------------------

def test_the_retrieval_example_indexes_and_searches():
    from agentkit.knowledge import Chunk, FtsIndex, Trust

    index = FtsIndex(sqlite3.connect(":memory:"), table="agent_knowledge")
    index.rebuild([Chunk(doc_id="doc:1", kind="note", title="Onboarding",
                         body="how to onboard", source="wiki",
                         trust=Trust.INTERNAL, ref="1")])
    assert index.search("onboarding", limit=8, min_trust=Trust.INTERNAL)


# --- §11: the documented fake -------------------------------------------------

def test_the_documented_fake_backend_actually_works():
    """§11 tells people how to write a test double. If this drifts, every reader's
    fake is wrong in the same way mine was."""
    from agentkit.llm.service import LLMService
    from agentkit.llm.types import ChatResult

    class FakeBackend:
        name, model = "fake", "m"

        def chat(self, req):
            return ChatResult(text="hi", tool_calls=(), stop_reason="stop",
                              provider=self.name, model=self.model)

    assert LLMService(backends=[FakeBackend()]).complete("s", "u") == "hi"


@pytest.mark.parametrize("module", [
    "llm/service.py", "llm/chain.py", "llm/probe.py", "llm/ledger.py", "llm/health.py",
    "llm/errors.py", "llm/capabilities.py", "llm/router.py", "llm/strategies.py",
    "llm/runner.py", "llm/types.py", "llm/jsonx.py",
    "tools.py", "guard.py", "permissions.py", "audit.py", "session.py", "knowledge.py",
])
def test_the_module_map_points_at_files_that_exist(module):
    """§10 is a map. A map to a file that moved is worse than no map."""
    assert f"`{module}`" in README.read_text(), f"README module map omits {module}"
    assert (README.parent / module).exists(), f"{module} does not exist"
