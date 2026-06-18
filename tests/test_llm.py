"""Multi-provider LLM client: chain ordering + automatic failover. No SDKs/network."""

import types

import pytest

from jobagent.llm_client import MultiLLM, build_llm


class FakeBackend:
    def __init__(self, name, *, output=None, raises=None):
        self.name = name
        self._output = output
        self._raises = raises
        self.calls = 0

    def generate(self, system, user):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._output


def test_uses_primary_when_it_works():
    a = FakeBackend("groq", output="hi from groq")
    b = FakeBackend("gemini", output="hi from gemini")
    llm = MultiLLM([a, b])
    assert llm.complete("s", "u") == "hi from groq"
    assert llm.last_provider == "groq"
    assert b.calls == 0                       # backup never touched


def test_falls_through_on_error():
    a = FakeBackend("groq", raises=RuntimeError("rate limit / quota exhausted"))
    b = FakeBackend("gemini", output="served by gemini")
    llm = MultiLLM([a, b])
    assert llm.complete("s", "u") == "served by gemini"
    assert llm.last_provider == "gemini"
    assert a.calls == 1 and b.calls == 1


def test_empty_response_counts_as_failure():
    a = FakeBackend("groq", output="   ")
    b = FakeBackend("gemini", output="real")
    assert MultiLLM([a, b]).complete("s", "u") == "real"


def test_all_fail_raises_with_detail():
    a = FakeBackend("groq", raises=RuntimeError("boom"))
    b = FakeBackend("gemini", raises=RuntimeError("kaboom"))
    with pytest.raises(RuntimeError) as e:
        MultiLLM([a, b]).complete("s", "u")
    assert "groq" in str(e.value) and "gemini" in str(e.value)


def test_json_mode_strips_code_fences():
    a = FakeBackend("groq", output='```json\n{"score": 0.9}\n```')
    assert MultiLLM([a]).complete("s", "u", json_mode=True) == '{"score": 0.9}'


def _settings(**kw):
    base = dict(
        llm_provider="groq", groq_api_key="", openrouter_api_key="", openai_api_key="",
        gemini_api_key="", anthropic_api_key="", groq_model="g", openrouter_model="o",
        openai_model="oa", gemini_model="ge", anthropic_model="an",
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_build_llm_orders_primary_first_then_free_backups():
    s = _settings(llm_provider="gemini", groq_api_key="k", gemini_api_key="k", openrouter_api_key="k")
    llm = build_llm(s)
    assert llm.chain == ["gemini", "groq", "openrouter"]   # primary first, then default order


def test_build_llm_skips_providers_without_keys():
    s = _settings(llm_provider="groq", groq_api_key="k")    # only groq has a key
    llm = build_llm(s)
    assert llm.chain == ["groq"]


def test_build_llm_none_when_no_keys():
    assert build_llm(_settings()) is None


def test_paid_primary_keeps_free_backups():
    # Future: paid anthropic primary, free groq/gemini remain as backups.
    s = _settings(llm_provider="anthropic", anthropic_api_key="k", groq_api_key="k", gemini_api_key="k")
    assert build_llm(s).chain == ["anthropic", "groq", "gemini"]
