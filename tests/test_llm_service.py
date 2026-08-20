"""The standalone multi-LLM service: pre-flight, routing, and the trace ledger.

Everything here uses fake backends. The point of the service is that it is reusable and
host-independent, so the tests build it from a `SimpleNamespace` rather than this
project's Settings — if these ever needed `jobagent`, the claim would be false.
"""

import time
from types import SimpleNamespace

import pytest

from agentkit.llm.errors import Verdict
from agentkit.llm.ledger import Ledger
from agentkit.llm.probe import order_by_health, probe_all, probe_backend
from agentkit.llm.service import AllProvidersFailed, LLMService
from agentkit.llm.types import ChatRequest, ChatResult


class FakeBackend:
    """Matches the REAL backend seam: `chat(ChatRequest) -> ChatResult`.

    An earlier version of this fake exposed `generate(system, user)` — a method no real
    backend has. Every test passed, against an interface that did not exist. The fake
    must mirror `agentkit/llm/backends/*`, not the author's memory of it; see
    `test_the_service_speaks_the_same_protocol_real_backends_expose` below, which is
    what would have caught it.
    """

    def __init__(self, name, model="m", answer="ok", error=None, delay=0.0):
        self.name, self.model = name, model
        self.answer, self.error, self.delay = answer, error, delay
        self.calls = 0
        self.seen: list[ChatRequest] = []

    def chat(self, req: ChatRequest) -> ChatResult:
        self.calls += 1
        self.seen.append(req)
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        # StopReason is a Literal, not an enum — a plain string. Reading the type
        # rather than assuming `StopReason.stop` is the same discipline as reading
        # store keys (R32); the assumed version raised AttributeError on every call.
        return ChatResult(text=self.answer, tool_calls=(), stop_reason="stop",
                          provider=self.name, model=self.model)


def _svc(*backends) -> LLMService:
    return LLMService(backends=list(backends))


# --- reusability --------------------------------------------------------------

def test_it_builds_from_any_object_with_the_right_attributes():
    """Duck-typed on purpose: a host with its own config class must not have to adopt
    pydantic, or ours, to use this."""
    cfg = SimpleNamespace(groq_api_key="k", groq_model="llama-3.3-70b", llm_provider="groq")
    service = LLMService.from_settings(cfg)
    assert service.chain == ["groq"]


def test_it_reports_why_a_provider_was_left_out():
    """"No chain" and "no GROQ_API_KEY" are the same symptom with very different fixes,
    so the reason travels with the result."""
    service = LLMService.from_settings(SimpleNamespace())
    assert service.chain == []
    reasons = dict(service.skipped)
    assert "groq" in reasons and "groq_api_key" in reasons["groq"]


def test_a_host_can_supply_its_own_provider_table():
    from agentkit.llm.chain import ProviderSpec

    spec = ProviderSpec("housebrand", "house_key", "house_model",
                        "https://example.invalid/v1", default_model="house-1")
    service = LLMService.from_providers([spec], SimpleNamespace(house_key="k"),
                                        order=("housebrand",))
    assert service.chain == ["housebrand"]


def test_cerebras_and_github_are_registered():
    cfg = SimpleNamespace(cerebras_api_key="a", github_models_token="b")
    assert set(LLMService.from_settings(cfg).chain) == {"cerebras", "github"}


# --- failover -----------------------------------------------------------------

def test_the_first_working_backend_answers():
    dead = FakeBackend("dead", error=RuntimeError("boom"))
    alive = FakeBackend("alive", answer="hello")
    assert _svc(dead, alive).complete("s", "u") == "hello"


def test_an_empty_response_counts_as_a_failure():
    """A 200 with an empty body is a failure that looks like success — how a
    misconfigured proxy passes a naive health check."""
    empty = FakeBackend("empty", answer="   ")
    alive = FakeBackend("alive", answer="real")
    assert _svc(empty, alive).complete("s", "u") == "real"


def test_total_failure_names_every_verdict():
    """"All providers failed" alone sends an operator hunting a code fault when the
    cause is usually one expired key."""
    a = FakeBackend("a", error=RuntimeError("401 invalid api key"))
    b = FakeBackend("b", error=RuntimeError("429 rate limit"))
    with pytest.raises(AllProvidersFailed) as exc:
        _svc(a, b).complete("s", "u")
    assert len(exc.value.attempts) == 2
    assert "a/" in str(exc.value) and "b/" in str(exc.value)


def test_a_dead_backend_is_skipped_on_the_next_call_rather_than_retried():
    """The breaker is the difference between failover and thrashing: without memory a
    bad key costs a full round trip on every single call, forever."""
    dead = FakeBackend("dead", error=RuntimeError("401 invalid api key"))
    alive = FakeBackend("alive")
    service = _svc(dead, alive)

    service.complete("s", "u")
    calls_after_first = dead.calls
    service.complete("s", "u")
    assert dead.calls == calls_after_first, "a permanently-dead backend was retried"


def test_json_mode_strips_code_fences():
    fenced = FakeBackend("f", answer='```json\n{"a": 1}\n```')
    assert _svc(fenced).complete("s", "u", json_mode=True) == '{"a": 1}'


# --- the ledger ---------------------------------------------------------------

def test_the_ledger_records_successes_failures_and_verdicts():
    dead = FakeBackend("dead", error=RuntimeError("401 invalid api key"))
    alive = FakeBackend("alive")
    service = _svc(dead, alive)
    service.complete("s", "u")

    data = service.ledger.as_dict()
    backends = {b["provider"]: b for b in data["backends"]}
    assert backends["alive"]["health"] == "ok"
    assert backends["dead"]["health"] == "dead"
    assert backends["dead"]["by_verdict"], "the failure was not classified"
    assert "alive/m" in data["working"] and "dead/m" in data["broken"]


def test_an_untried_backend_is_not_reported_as_healthy():
    """A vacuous 100% is how a provider nobody has called reads as fine — the same trap
    the assistant eval hit when it printed "grounding 100%" over zero graded cases."""
    ledger = Ledger()
    ledger.record("a", "m", ok=True, latency_s=0.1)
    stats = {s.provider: s for s in ledger.stats()}
    assert stats["a"].success_rate == 1.0

    fresh = Ledger()
    from agentkit.llm.ledger import ProviderStats

    assert ProviderStats("x", "m").success_rate is None
    assert ProviderStats("x", "m").health == "untried"
    assert fresh.render() == "no provider calls recorded yet"


def test_worst_health_sorts_first():
    """The thing to look at should be on top of the report, not buried under the
    providers that are fine."""
    ledger = Ledger()
    ledger.record("good", "m", ok=True, latency_s=0.1)
    ledger.record("bad", "m", ok=False, latency_s=0.1, verdict=Verdict.PERMANENT, detail="401")
    assert ledger.stats()[0].provider == "bad"


def test_error_detail_is_trimmed_and_single_lined():
    """Provider error bodies run to kilobytes of JSON. One pasted whole into a tool
    result is how a model ends up quoting a key back at someone."""
    ledger = Ledger()
    ledger.record("a", "m", ok=False, latency_s=0.1, verdict=Verdict.UNKNOWN,
                  detail="x" * 500 + "\nsecond line")
    stats = ledger.stats()[0]
    assert "\n" not in stats.last_error
    assert len(stats.last_error) < 200


def test_a_backend_that_fails_then_recovers_reads_as_degraded():
    ledger = Ledger()
    ledger.record("a", "m", ok=False, latency_s=0.1, verdict=Verdict.TRANSIENT)
    ledger.record("a", "m", ok=True, latency_s=0.1)
    assert ledger.stats()[0].health == "degraded"


def test_render_is_readable_and_names_the_error():
    ledger = Ledger()
    ledger.record("groq", "llama", ok=False, latency_s=0.4,
                  verdict=Verdict.CAPABILITY, detail="model_not_found")
    out = ledger.render()
    assert "groq/llama" in out and "model_not_found" in out and "✗" in out


# --- pre-flight ---------------------------------------------------------------

def test_probe_never_raises_and_classifies_the_failure():
    result = probe_backend(FakeBackend("x", error=RuntimeError("401 invalid api key")))
    assert result.ok is False and result.verdict
    assert "RuntimeError" in result.detail


def test_probe_treats_an_empty_answer_as_down():
    assert probe_backend(FakeBackend("x", answer="")).ok is False


def test_probe_all_reports_both_sides_and_records_them():
    ledger = Ledger()
    report = probe_all([FakeBackend("up"), FakeBackend("down", error=RuntimeError("boom"))],
                       ledger=ledger)
    assert [r.provider for r in report.working] == ["up"]
    assert [r.provider for r in report.broken] == ["down"]
    assert len(ledger.stats()) == 2, "the probe did not feed the ledger"


def test_probe_runs_concurrently_not_one_at_a_time():
    """Sequential probing costs the sum of every provider's latency, which is exactly
    the cost this is meant to avoid."""
    slow = [FakeBackend(f"s{i}", delay=0.25) for i in range(4)]
    started = time.monotonic()
    probe_all(slow, max_workers=4)
    elapsed = time.monotonic() - started
    assert elapsed < 0.25 * 3, f"probes look sequential ({elapsed:.2f}s for 4 x 0.25s)"


def test_preflight_reorders_by_measured_latency():
    slow = FakeBackend("slow", delay=0.2)
    fast = FakeBackend("fast")
    service = _svc(slow, fast)
    service.preflight(timeout_s=5)
    assert service.chain[0] == "fast", "the faster working backend should route first"


def test_preflight_never_empties_the_chain():
    """Reordering changes sequence, never membership. A probe is a snapshot, and a
    provider that failed one health check may still serve the next request."""
    a = FakeBackend("a", error=RuntimeError("boom"))
    b = FakeBackend("b", error=RuntimeError("boom"))
    service = _svc(a, b)
    service.preflight(timeout_s=5)
    assert sorted(service.chain) == ["a", "b"]


def test_dead_backends_sort_last_but_stay_reachable():
    up, down = FakeBackend("up"), FakeBackend("down", error=RuntimeError("boom"))
    report = probe_all([down, up])
    assert [b.name for b in order_by_health([down, up], report)] == ["up", "down"]


def test_preflight_report_says_so_when_nothing_is_reachable():
    """The single most actionable line this tool can print."""
    report = probe_all([FakeBackend("a", error=RuntimeError("boom"))])
    assert "NOTHING is reachable" in report.render()


def test_describe_shows_the_order_and_the_health_together():
    service = _svc(FakeBackend("a"))
    service.complete("s", "u")
    out = service.describe()
    assert "chain: a" in out and "a/m" in out


# --- interface conformance ----------------------------------------------------

def test_the_service_speaks_the_same_protocol_real_backends_expose():
    """The guard for the bug above: the fakes here must not drift from the real seam.

    Asserts against the actual backend classes rather than a remembered description —
    if `chat` is ever renamed or given a different shape, this fails instead of every
    test in the file passing against an interface nobody implements.
    """
    import inspect

    from agentkit.llm.backends.anthropic_chat import AnthropicChat
    from agentkit.llm.backends.openai_compat import OpenAICompatChat

    for cls in (OpenAICompatChat, AnthropicChat):
        assert hasattr(cls, "chat"), f"{cls.__name__} no longer exposes chat()"
        assert not hasattr(cls, "generate"), (
            f"{cls.__name__} grew a generate() — the service and fakes assume chat()"
        )
        params = list(inspect.signature(cls.chat).parameters)
        assert params[:2] == ["self", "req"], f"{cls.__name__}.chat signature changed"

    # And the fake must satisfy the same shape.
    assert hasattr(FakeBackend, "chat") and not hasattr(FakeBackend, "generate")


def test_the_probe_sends_a_real_request_not_a_ping():
    """`llm_doctor --probe` once called a provider healthy when it could not serve a
    real request — "reply ok" fits in a quota where a genuine prompt does not."""
    backend = FakeBackend("x")
    probe_backend(backend)
    req = backend.seen[0]
    assert req.messages and req.messages[0].text.strip(), "probe sent an empty prompt"
    assert req.system, "probe sent no system prompt"


# --- the probe must not condemn a healthy provider ----------------------------

class LengthCappedBackend:
    """A reasoning-style model that spends its budget before emitting visible text."""

    name, model = "reasoner", "gpt-oss-20b"

    def __init__(self, budget_needed: int = 30):
        self.budget_needed = budget_needed

    def chat(self, req: ChatRequest) -> ChatResult:
        if req.max_tokens < self.budget_needed:
            return ChatResult(text="", tool_calls=(), stop_reason="length",
                              provider=self.name, model=self.model)
        return ChatResult(text="ready", tool_calls=(), stop_reason="stop",
                          provider=self.name, model=self.model)


def test_a_starved_token_budget_is_not_reported_as_a_dead_provider():
    """Measured on groq/openai/gpt-oss-20b: 16 output tokens returned an EMPTY string
    with stop_reason="length"; 64 returned "ready" using 30. The first version of this
    probe used 16 and declared all three live providers dead — a diagnostic that would
    have had someone rotating perfectly good API keys.
    """
    starved = probe_backend(LengthCappedBackend(), max_tokens=8)
    assert starved.ok is True, "a length-capped answer is OUR fault, not the provider's"
    assert "not enough" in starved.detail

    fine = probe_backend(LengthCappedBackend(), max_tokens=128)
    assert fine.ok is True and fine.detail == ""


def test_a_genuinely_empty_answer_is_still_a_failure():
    """The distinction that makes the above safe: stop_reason tells the two apart. A
    200 with an empty body and a normal stop is how a broken proxy passes a naive
    health check."""

    class EmptyBackend:
        name, model = "proxy", "m"

        def chat(self, req):
            return ChatResult(text="", tool_calls=(), stop_reason="stop",
                              provider="proxy", model="m")

    result = probe_backend(EmptyBackend())
    assert result.ok is False and "empty response" in result.detail


def test_the_default_probe_budget_leaves_room_for_reasoning_tokens():
    """Pinning the constant: dropping it back to a small number silently reintroduces
    the bug, and the symptom is 'all my providers are down'."""
    from agentkit.llm.probe import PROBE_MAX_TOKENS

    assert PROBE_MAX_TOKENS >= 64
