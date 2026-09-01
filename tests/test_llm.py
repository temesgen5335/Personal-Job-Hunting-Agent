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


# --- new providers ---------------------------------------------------------------

def test_new_openai_compatible_providers_register_when_keyed():
    s = _settings(llm_provider="groq", groq_api_key="k",
                  sambanova_api_key="k", mistral_api_key="k", nvidia_api_key="k")
    # order follows _DEFAULT_ORDER: groq, …, sambanova, nvidia, mistral, …
    assert build_llm(s).chain == ["groq", "sambanova", "nvidia", "mistral"]


def test_pollinations_is_keyless_but_opt_in():
    on = _settings(llm_provider="groq", groq_api_key="k", pollinations_enabled=True)
    assert "pollinations" in build_llm(on).chain
    off = _settings(llm_provider="groq", groq_api_key="k")   # flag unset → False
    assert "pollinations" not in build_llm(off).chain


def test_no_keys_still_none_even_though_pollinations_exists():
    assert build_llm(_settings()) is None   # keyless provider is opt-in, so still None


# --- OpenRouter free-model discovery + fan-out -----------------------------------

def test_rank_free_keeps_free_chat_models_tools_and_big_context_first():
    from jobagent.llm_client import _rank_free
    text = {"input_modalities": ["text"], "output_modalities": ["text"]}
    models = [
        {"id": "a/small:free", "context_length": 8000, "architecture": text, "supported_parameters": []},
        {"id": "a/big:free", "context_length": 1_000_000, "architecture": text, "supported_parameters": ["tools"]},
        {"id": "a/audio:free", "context_length": 9, "architecture": {"input_modalities": ["text"], "output_modalities": ["audio"]}},
        {"id": "a/paid", "context_length": 9, "architecture": text},  # not :free
    ]
    assert _rank_free(models) == ["a/big:free", "a/small:free"]   # tools+big first; audio & paid dropped


def test_openrouter_free_models_returns_empty_on_fetch_failure(monkeypatch):
    import httpx
    from jobagent import llm_client
    llm_client._free_cache.update(at=-1e18, ids=None)   # bust the cache

    def boom(*a, **k):
        raise httpx.ConnectError("no network")
    monkeypatch.setattr(httpx, "get", boom)
    assert llm_client.openrouter_free_models("k") == []   # degrades, never raises


def test_build_llm_fans_out_over_live_free_models(monkeypatch):
    monkeypatch.setattr("jobagent.llm_client.openrouter_free_models",
                        lambda api_key, limit=6: ["nvidia/nemotron:free", "z-ai/glm-5.2:free"])
    s = _settings(llm_provider="openrouter", openrouter_api_key="k", openrouter_free_fanout=True)
    assert build_llm(s).chain == ["openrouter:nemotron", "openrouter:glm-5.2"]


def test_openrouter_stays_single_model_when_fanout_off():
    s = _settings(llm_provider="openrouter", openrouter_api_key="k")   # fanout unset → off
    assert build_llm(s).chain == ["openrouter"]


def test_fanout_falls_back_to_configured_model_when_list_empty(monkeypatch):
    monkeypatch.setattr("jobagent.llm_client.openrouter_free_models",
                        lambda api_key, limit=6: [])   # fetch failed
    s = _settings(llm_provider="openrouter", openrouter_api_key="k", openrouter_free_fanout=True)
    assert build_llm(s).chain == ["openrouter"]   # single configured model, not empty
