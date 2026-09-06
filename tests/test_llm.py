"""jobagent's `build_llm` adapter over agentkit's reusable `LLMService`.

The router itself — ordered failover, circuit breaker, provider registry, and the live
OpenRouter free-model fan-out — is tested in `tests/test_agentkit_llm.py` and
`tests/test_llm_service.py`. This file asserts only the thin adapter contract the pipeline
relies on: build a service from settings, thread the deterministic temperature, and return
None when nothing is usable."""

import types

from agentkit.llm.service import LLMService
from jobagent.llm_client import build_llm


def _settings(**kw):
    base = dict(
        llm_provider="groq", groq_api_key="", openrouter_api_key="", openai_api_key="",
        gemini_api_key="", anthropic_api_key="", groq_model="g", openrouter_model="o",
        openai_model="oa", gemini_model="ge", anthropic_model="an",
    )
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_build_llm_returns_an_agentkit_service():
    llm = build_llm(_settings(groq_api_key="k"))
    assert isinstance(llm, LLMService)
    assert llm.chain == ["groq"]


def test_build_llm_threads_the_deterministic_temperature():
    assert build_llm(_settings(groq_api_key="k")).temperature == 0.3     # scoring default
    assert build_llm(_settings(groq_api_key="k"), temperature=0.0).temperature == 0.0


def test_build_llm_is_none_when_no_provider_is_usable():
    assert build_llm(_settings()) is None


def test_build_llm_orders_primary_first_then_free_backups():
    llm = build_llm(_settings(llm_provider="gemini", groq_api_key="k",
                              gemini_api_key="k", openrouter_api_key="k"))
    assert llm.chain == ["gemini", "groq", "openrouter"]   # primary first, then default order


def test_build_llm_skips_providers_without_keys():
    assert build_llm(_settings(groq_api_key="k")).chain == ["groq"]
