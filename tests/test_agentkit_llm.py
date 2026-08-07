"""agentkit LLM layer: IR, provider translation, tiering, error classification.

No network and no SDKs required — the translation functions are pure, and the backends
are exercised through fakes. What this pins down is the stuff that silently breaks in
production: message-shape asymmetries between providers, malformed tool arguments, and
failover treating a dead API key like a rate limit.
"""

import json
import sys
from pathlib import Path

import pytest

from agentkit.llm import (
    ChatRequest,
    Message,
    Tier,
    ToolCall,
    ToolResult,
    ToolSpec,
    Verdict,
    assert_wellformed,
    classify,
    resolve_card,
    validate_tool_schema,
)
from agentkit.llm.backends.anthropic_chat import to_anthropic_messages, to_anthropic_tools
from agentkit.llm.backends.openai_compat import (
    _parse_args,
    to_openai_messages,
    to_openai_tools,
)

SEARCH = ToolSpec("search_jobs", "Search stored postings.", {
    "type": "object",
    "properties": {"query": {"type": "string", "description": "keyword"}},
    "required": ["query"],
})


# --- the boundary that makes this package reusable ---------------------------------

def _agentkit_modules():
    root = Path(__file__).resolve().parent.parent / "src" / "agentkit"
    return sorted(root.rglob("*.py"))


def _module_level_imports(path):
    """Import roots at module scope only — a lazy import inside a function does not
    make the module require the package."""
    import ast
    tree = ast.parse(path.read_text())
    roots = []
    for node in tree.body:                      # top level only, not ast.walk
        if isinstance(node, ast.Import):
            roots += [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.append(node.module.split(".")[0])
    return roots


def test_agentkit_never_imports_the_host_application():
    """The whole point of the package. If agentkit imports jobagent it is not a
    harness, it is a feature of one app.

    Provider SDKs are a separate question — see the two tests below. What can never
    appear anywhere, at any scope, is the domain.
    """
    import ast
    offenders = []
    for path in _agentkit_modules():
        for node in ast.walk(ast.parse(path.read_text())):
            roots = []
            if isinstance(node, ast.Import):
                roots = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                roots = [node.module.split(".")[0]]
            offenders += [f"{path.name}: {r}" for r in roots
                          if r in ("jobagent", "fastapi", "telegram", "telethon", "playwright")]
    assert offenders == [], f"agentkit must stay domain-agnostic: {offenders}"


def test_agentkit_core_imports_without_any_provider_sdk():
    """Everything outside backends/ must load on a bare stdlib+pydantic install, so the
    harness is usable with a provider we haven't written a backend for."""
    core = [p for p in _agentkit_modules() if "backends" not in p.parts]
    offenders = [f"{p.name}: {r}" for p in core for r in _module_level_imports(p)
                 if r not in ("agentkit", "pydantic") and r not in sys.stdlib_module_names]
    assert offenders == [], f"agentkit core must not require an SDK: {offenders}"


def test_backend_sdk_imports_are_lazy():
    """A backend may reference its SDK, but only inside a function — importing
    agentkit.llm.backends.openai_compat must not require `openai` to be installed."""
    backends = [p for p in _agentkit_modules() if "backends" in p.parts]
    assert backends, "expected backend modules to exist"
    offenders = [f"{p.name}: {r}" for p in backends for r in _module_level_imports(p)
                 if r not in ("agentkit", "pydantic") and r not in sys.stdlib_module_names]
    assert offenders == [], f"SDK imports must be lazy, not module-level: {offenders}"


def test_agentkit_carries_no_domain_vocabulary():
    """Import checks catch coupling; this catches the slow leak where a job-specific
    special case grows into generic code."""
    banned = ("job_posting", "cv_master", "applicant", "employer", "recruiter")
    hits = [f"{p.name}:{w}" for p in _agentkit_modules()
            for w in banned if w in p.read_text().lower()]
    assert hits == [], f"domain vocabulary leaked into the generic package: {hits}"


# --- tool schema portability -------------------------------------------------------

def test_a_portable_schema_passes():
    assert validate_tool_schema(SEARCH) == []


def test_unportable_constructs_are_rejected_at_registration():
    """Gemini's OpenAI-compat endpoint and grammar-constrained local servers reject
    these, and weak models fail hardest on nesting — so this is a build-time error,
    not a runtime mystery."""
    bad = ToolSpec("x", "d", {"type": "object", "properties": {
        "a": {"anyOf": [{"type": "string"}], "description": "d"}}, "anyOf": []})
    assert any("anyOf" in p for p in validate_tool_schema(bad))

    deep = ToolSpec("x", "d", {"type": "object", "properties": {
        "a": {"type": "object", "description": "d", "properties": {
            "b": {"type": "object", "description": "d", "properties": {}}}}}})
    assert any("nesting" in p for p in validate_tool_schema(deep))


def test_properties_must_be_described_because_the_model_reads_them():
    undescribed = ToolSpec("x", "d", {"type": "object",
                                      "properties": {"a": {"type": "string"}}})
    assert any("description" in p for p in validate_tool_schema(undescribed))


def test_required_must_reference_a_real_property():
    bogus = ToolSpec("x", "d", {"type": "object", "properties": {}, "required": ["nope"]})
    assert any("not in properties" in p for p in validate_tool_schema(bogus))


# --- OpenAI translation ------------------------------------------------------------

def test_grouped_tool_results_expand_to_one_openai_message_each():
    req = ChatRequest(system="S", messages=[
        Message("user", "hi"),
        Message("assistant", "", tool_calls=(ToolCall("c1", "a"), ToolCall("c2", "b"))),
        Message("tool", tool_results=(ToolResult("c1", "a", "1"), ToolResult("c2", "b", "2"))),
    ])
    out = to_openai_messages(req)
    assert out[0] == {"role": "system", "content": "S"}
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "tool"]
    assert [m["tool_call_id"] for m in out if m["role"] == "tool"] == ["c1", "c2"]


def test_openai_tool_choice_forms():
    req = ChatRequest(messages=[], tools=(SEARCH,), tool_choice="auto")
    assert to_openai_tools(req)[1] == "auto"
    req.tool_choice = "search_jobs"
    assert to_openai_tools(req)[1] == {"type": "function", "function": {"name": "search_jobs"}}


def test_malformed_tool_arguments_never_raise():
    """A model emitting broken JSON is information the loop can act on; an exception
    would lose the whole turn."""
    args, err = _parse_args('{"query": "go"')
    assert args == {} and "invalid JSON" in err
    args, err = _parse_args('["not", "an", "object"]')
    assert args == {} and "must be an object" in err
    args, err = _parse_args('{"query": "go"}')
    assert args == {"query": "go"} and err == ""


@pytest.mark.parametrize("raw", ["", "{}", "null", "  null  "])
def test_every_spelling_of_no_arguments_is_accepted(raw):
    """Groq returns the literal string "null" for a no-parameter tool — observed live
    against llama-3.3-70b, and something no fake would have reproduced. Reporting a
    parse_error here would cost a whole turn telling the model to fix nothing."""
    args, err = _parse_args(raw)
    assert args == {} and err == ""


# --- Anthropic translation ---------------------------------------------------------

def test_anthropic_groups_results_into_one_user_message():
    """Anthropic rejects results split across messages; the IR's grouping makes this
    a fold rather than a forward scan."""
    req = ChatRequest(messages=[
        Message("assistant", "", tool_calls=(ToolCall("c1", "a"), ToolCall("c2", "b"))),
        Message("tool", tool_results=(ToolResult("c1", "a", "1"), ToolResult("c2", "b", "2"))),
    ])
    out = to_anthropic_messages(req)
    assert len(out) == 2
    assert out[1]["role"] == "user"
    assert [b["tool_use_id"] for b in out[1]["content"]] == ["c1", "c2"]


def test_anthropic_tool_use_blocks_carry_parsed_input():
    req = ChatRequest(messages=[
        Message("assistant", "thinking", tool_calls=(ToolCall("c1", "search_jobs",
                                                              {"query": "go"}),))])
    blocks = to_anthropic_messages(req)[0]["content"]
    assert blocks[0]["type"] == "text"
    assert blocks[1] == {"type": "tool_use", "id": "c1", "name": "search_jobs",
                         "input": {"query": "go"}}


def test_anthropic_tool_choice_mapping():
    req = ChatRequest(messages=[], tools=(SEARCH,), tool_choice="required")
    assert to_anthropic_tools(req)[1] == {"type": "any"}
    req.tool_choice = "search_jobs"
    assert to_anthropic_tools(req)[1] == {"type": "tool", "name": "search_jobs"}
    req.tool_choice = "none"
    assert to_anthropic_tools(req) == (None, None)     # no "none" — withhold the tools


def test_system_is_a_field_not_a_message_for_anthropic():
    req = ChatRequest(system="S", messages=[Message("user", "hi")])
    assert all(m["role"] != "system" for m in to_anthropic_messages(req))


# --- history well-formedness -------------------------------------------------------

def test_orphan_tool_result_is_caught_before_the_provider_400s():
    """Truncating history mid-turn produces a 400 that reads like a model failure."""
    with pytest.raises(ValueError, match="answers no call"):
        assert_wellformed([
            Message("assistant", "", tool_calls=(ToolCall("c1", "a"),)),
            Message("tool", tool_results=(ToolResult("cX", "a", "1"),)),
        ])
    with pytest.raises(ValueError, match="directly follow"):
        assert_wellformed([Message("tool", tool_results=(ToolResult("c1", "a", "1"),))])


# --- tiering -----------------------------------------------------------------------

def test_measured_models_resolve_to_their_measured_capability():
    """Seeded from a real spike. The 8B is the important row: it emits perfect tool
    calls but cannot use a tool result, so native_tools alone would mislead."""
    weak = resolve_card("groq", "llama-3.1-8b-instant")
    assert weak.tier is Tier.WEAK
    assert weak.native_tools is True and weak.tool_loop is False

    strong = resolve_card("groq", "llama-3.3-70b-versatile")
    assert strong.tier is Tier.STANDARD and strong.tool_loop is True


def test_unknown_model_is_unknown_not_guessed():
    card = resolve_card("custom", "some-local-model-v9")
    assert card.tier is Tier.UNKNOWN
    assert card.native_tools is None
    assert "LLM_TIER_OVERRIDES" in card.notes      # tells the user how to fix it


def test_model_id_spellings_fold_to_one_registry_entry():
    for spelling in ("meta-llama/llama-3.3-70b-instruct:free",
                     "Llama-3.3-70B-Instruct",
                     "llama-3.3-70b-instruct-20250101"):
        assert resolve_card("openai", spelling).tier is Tier.STANDARD, spelling


def test_provider_overlay_beats_the_model_entry():
    """Same weights via OpenRouter :free route to varying upstreams, so capability is
    unproven even though the model is known."""
    assert resolve_card("openrouter", "llama-3.3-70b-instruct").native_tools is None
    assert resolve_card("groq", "llama-3.3-70b-versatile").native_tools is True


def test_settings_override_wins_over_everything():
    from types import SimpleNamespace
    s = SimpleNamespace(llm_tier_overrides="custom=standard,groq:llama-3.1-8b-instant=strong")
    assert resolve_card("custom", "unknown-thing", s).tier is Tier.STANDARD
    assert resolve_card("groq", "llama-3.1-8b-instant", s).tier is Tier.STRONG


def test_resolve_card_tolerates_a_minimal_settings_object():
    """The existing LLM tests pass a SimpleNamespace with a handful of attributes; every
    settings read here must use getattr with a default or the suite breaks."""
    from types import SimpleNamespace
    assert resolve_card("groq", "llama-3.1-8b-instant", SimpleNamespace()).tier is Tier.WEAK
    assert resolve_card("groq", "llama-3.1-8b-instant", None).tier is Tier.WEAK


def test_a_malformed_override_is_ignored_not_fatal():
    from types import SimpleNamespace
    s = SimpleNamespace(llm_tier_overrides="garbage,,custom=nonsense,custom2=weak")
    assert resolve_card("custom", "x", s).tier is Tier.UNKNOWN     # bad value skipped
    assert resolve_card("custom2", "x", s).tier is Tier.WEAK       # good one still applies


# --- error classification ----------------------------------------------------------

class _Err(Exception):
    def __init__(self, msg, status=None, headers=None):
        super().__init__(msg)
        if status is not None:
            self.status_code = status
        if headers is not None:
            self.response = type("R", (), {"status_code": status, "headers": headers})()


def test_a_dead_api_key_is_permanent_not_retryable():
    """MultiLLM retries a 401 on every call forever; this is the fix."""
    c = classify(_Err("Incorrect API key", 401))
    assert c.verdict is Verdict.PERMANENT
    assert not c.retryable_same_backend


def test_rate_limit_carries_retry_after_capped():
    c = classify(_Err("slow down", 429, {"retry-after": "12"}))
    assert c.verdict is Verdict.RATE_LIMIT and c.retry_after_s == 12.0
    huge = classify(_Err("slow down", 429, {"retry-after": "99999"}))
    assert huge.retry_after_s == 300.0                    # capped, can't wedge the process


def test_http_date_retry_after_degrades_to_our_own_backoff():
    c = classify(_Err("slow", 429, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}))
    assert c.verdict is Verdict.RATE_LIMIT and c.retry_after_s is None


def test_capability_and_context_are_distinguished_from_bad_request():
    assert classify(_Err("model does not support tools", 400)).verdict is Verdict.CAPABILITY
    assert classify(_Err("context length exceeded", 400)).verdict is Verdict.CONTEXT
    assert classify(_Err("model not found", 404)).verdict is Verdict.CAPABILITY


def test_server_errors_and_timeouts_are_transient():
    assert classify(_Err("bad gateway", 502)).verdict is Verdict.TRANSIENT
    assert classify(_Err("boom", 500)).verdict is Verdict.TRANSIENT


def test_sdk_exception_types_are_matched_without_importing_the_sdk():
    """errors.py must import with no provider SDK installed, so types are matched by
    name rather than isinstance."""
    for name, expected in (("RateLimitError", Verdict.RATE_LIMIT),
                           ("AuthenticationError", Verdict.PERMANENT),
                           ("APITimeoutError", Verdict.TRANSIENT),
                           ("NotFoundError", Verdict.CAPABILITY)):
        exc = type(name, (Exception,), {})("x")
        assert classify(exc).verdict is expected, name


def test_unrecognized_failures_are_unknown_not_permanent():
    """Guessing PERMANENT on an unfamiliar error would disable a working provider."""
    assert classify(Exception("something odd happened")).verdict is Verdict.UNKNOWN


def test_tool_spec_json_schema_survives_a_round_trip():
    """The schema goes over the wire verbatim; it must be plain JSON."""
    assert json.loads(json.dumps(SEARCH.parameters)) == SEARCH.parameters


# --- adding a provider key must Just Work -------------------------------------------
# A registry that only knows the strings someone happened to test degrades every new
# model to the slow path. These pin the layered resolution that prevents that.

@pytest.mark.parametrize("model,tier,tools", [
    # Anthropic
    ("claude-sonnet-4-6", Tier.STRONG, True),
    ("claude-3-5-sonnet-latest", Tier.STRONG, True),
    ("claude-opus-4-1-20250805", Tier.STRONG, True),
    ("claude-3-5-haiku-latest", Tier.STANDARD, True),
    # OpenAI
    ("gpt-4o", Tier.STRONG, True),
    ("gpt-4.1", Tier.STRONG, True),
    ("gpt-4o-mini", Tier.STANDARD, True),
    ("gpt-4.1-mini", Tier.STANDARD, True),
    # Google
    ("gemini-2.0-flash", Tier.STANDARD, True),
    ("gemini-2.5-flash", Tier.STANDARD, True),
    ("gemini-2.5-pro", Tier.STRONG, True),
    # Qwen
    ("qwen-max", Tier.STRONG, True),
    ("qwen-plus", Tier.STANDARD, True),
    ("qwen-turbo", Tier.WEAK, True),
    # others reachable through OpenRouter or locally
    ("deepseek-chat", Tier.STANDARD, True),
    ("mistral-large-latest", Tier.STRONG, True),
])
def test_every_named_provider_family_resolves_without_being_measured(model, tier, tools):
    card = resolve_card("someprovider", model)
    assert card.tier is tier, f"{model} → {card.tier}"
    assert card.native_tools is tools
    assert card.source != "default", f"{model} fell through to UNKNOWN"


@pytest.mark.parametrize("model,tier", [
    ("llama-3.2-3b", Tier.TINY),
    ("qwen2.5-7b-instruct", Tier.WEAK),
    ("qwen/qwen3.6-27b", Tier.STANDARD),
    ("meta-llama/llama-3.3-70b-instruct", Tier.STANDARD),
    ("some-vendor-13b-chat", Tier.WEAK),
])
def test_tier_is_inferred_from_parameter_size_for_open_models(model, tier):
    """So an unseen open model — a new Qwen, a local Ollama build — routes sensibly
    instead of falling to UNKNOWN and being permanently degraded."""
    assert resolve_card("local", model).tier is tier


def test_open_family_supplies_capability_while_size_supplies_tier():
    """A llama-3 at 8B and at 70B are the same family and very different models: tool
    support is a family fact, tier is a size fact."""
    big = resolve_card("local", "meta-llama/llama-3.3-70b-instruct")
    small = resolve_card("local", "qwen2.5-7b-instruct")
    assert big.native_tools is True and big.tier is Tier.STANDARD and big.tool_loop is True
    assert small.native_tools is True and small.tier is Tier.WEAK
    assert small.tool_loop is False       # derived: emitting a call ≠ using a result


def test_version_bumps_and_vendor_prefixes_do_not_regress_a_model():
    """Model ids churn; a date stamp or an Ollama tag must not drop a known model to
    UNKNOWN the day a provider renames it."""
    for spelling in ("claude-sonnet-4-6-20260101", "anthropic/claude-sonnet-4-6",
                     "claude-sonnet-4-6-latest"):
        assert resolve_card("anthropic", spelling).tier is Tier.STRONG, spelling
    assert resolve_card("custom", "qwen2.5-72b-instruct@q4").tier is Tier.STANDARD


def test_reasoning_models_do_not_claim_unverified_tool_support():
    """o-series and deepseek-r1 vary by version; claiming tools would route real work
    to a model that may reject the parameter."""
    for m in ("o1-mini", "o3", "deepseek-reasoner"):
        assert resolve_card("x", m).native_tools is None, m


def test_a_measurement_always_beats_a_derivation():
    """llama-3.1-8b would derive tool_loop=False from its tier anyway — but the point
    is that an explicit measurement is authoritative, not coincidentally agreeing."""
    card = resolve_card("groq", "llama-3.1-8b-instant")
    assert card.source == "measured"
    assert card.tool_loop_measured is False and card.tool_loop is False


# --- chain assembly ----------------------------------------------------------------

def _settings(**kw):
    from types import SimpleNamespace
    base = dict(llm_provider="groq", groq_api_key="", gemini_api_key="", openai_api_key="",
                anthropic_api_key="", qwen_api_key="", openrouter_api_key="",
                custom_llm_base_url="", custom_llm_api_key="", custom_llm_model="")
    base.update(kw)
    return SimpleNamespace(**base)


def test_a_provider_appears_as_soon_as_it_has_a_key():
    from agentkit.llm import build_chain
    assert build_chain(_settings()) == []
    chain = build_chain(_settings(anthropic_api_key="k"))
    assert [b.name for b in chain] == ["anthropic"]
    assert chain[0].model == "claude-sonnet-4-6"      # spec default when unset


def test_all_five_providers_assemble_together():
    from agentkit.llm import build_chain
    chain = build_chain(_settings(groq_api_key="k", gemini_api_key="k", openai_api_key="k",
                                  anthropic_api_key="k", qwen_api_key="k"))
    assert {b.name for b in chain} == {"groq", "gemini", "openai", "anthropic", "qwen"}
    assert all(b.card.tier.name != "UNKNOWN" for b in chain), \
        "a configured provider resolving to UNKNOWN would be permanently degraded"


def test_the_configured_primary_leads_the_chain():
    from agentkit.llm import build_chain
    chain = build_chain(_settings(llm_provider="anthropic", groq_api_key="k",
                                  anthropic_api_key="k"))
    assert chain[0].name == "anthropic"


def test_a_local_endpoint_needs_a_url_not_a_key():
    from agentkit.llm import build_chain
    assert build_chain(_settings(custom_llm_api_key="k")) == []          # url is what matters
    chain = build_chain(_settings(custom_llm_base_url="http://localhost:11434/v1",
                                  custom_llm_model="qwen2.5-32b-instruct"))
    assert [b.name for b in chain] == ["custom"]
    assert chain[0].card.tier.name == "STANDARD"      # inferred from 32b


def test_the_report_explains_why_a_provider_was_skipped():
    from agentkit.llm import build_chain
    rep = build_chain(_settings(groq_api_key="k"), report=True)
    skipped = dict(rep.skipped)
    assert "anthropic" in skipped and "anthropic_api_key" in skipped["anthropic"]


def test_backends_are_constructed_without_importing_any_sdk():
    """build_chain must work on a bare install — the SDK is only needed to CALL."""
    from agentkit.llm import build_chain
    chain = build_chain(_settings(anthropic_api_key="k", openai_api_key="k"))
    assert len(chain) == 2 and all(b._client is None for b in chain)
