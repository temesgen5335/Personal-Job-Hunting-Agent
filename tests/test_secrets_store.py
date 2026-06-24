"""Encrypted secret store + settings overlay + custom LLM provider (v2.1)."""

import types

import pytest

from jobagent.secrets_store import SecretStore, masked_view


def _store(tmp_path):
    return SecretStore(path=str(tmp_path / "secrets.enc"), key=SecretStore.generate_key())


def test_roundtrip_encrypted(tmp_path):
    s = _store(tmp_path)
    s.save({"groq_api_key": "gsk_secret", "llm_provider": "groq"})
    # On disk it's ciphertext, not the plaintext key.
    assert b"gsk_secret" not in (tmp_path / "secrets.enc").read_bytes()
    assert s.load() == {"groq_api_key": "gsk_secret", "llm_provider": "groq"}


def test_no_file_returns_empty_no_key_needed(tmp_path):
    assert SecretStore(path=str(tmp_path / "nope.enc")).load() == {}   # no crypto/key needed


def test_update_merges_and_clears(tmp_path):
    s = _store(tmp_path)
    s.update({"groq_api_key": "a", "gemini_api_key": "b"})
    s.update({"groq_api_key": ""})            # empty clears
    got = s.load()
    assert "groq_api_key" not in got and got["gemini_api_key"] == "b"


def test_update_ignores_unknown_fields(tmp_path):
    s = _store(tmp_path)
    s.update({"groq_api_key": "a", "evil": "x"})
    assert "evil" not in s.load()


def test_masked_view_hides_secrets():
    view = masked_view({"groq_api_key": "secret", "llm_provider": "groq", "openai_api_key": ""})
    assert view["groq_api_key"] == {"set": True}
    assert view["openai_api_key"] == {"set": False}
    assert view["llm_provider"] == "groq"      # non-secret shown plainly


def test_settings_overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAGENT_SECRETS_PATH", str(tmp_path / "secrets.enc"))
    monkeypatch.setenv("JOBAGENT_MASTER_KEY", SecretStore.generate_key())
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    SecretStore().save({"groq_api_key": "from_store", "llm_provider": "groq"})

    import jobagent.config as cfg
    s = cfg.reload_settings()                  # env + store overlay
    assert s.groq_api_key == "from_store"
    cfg.reload_settings()                       # reset cache for other tests


def test_custom_provider_in_chain():
    from jobagent.llm_client import build_llm
    st = types.SimpleNamespace(
        llm_provider="custom", groq_api_key="", openrouter_api_key="", openai_api_key="",
        gemini_api_key="", anthropic_api_key="", groq_model="g", openrouter_model="o",
        openai_model="oa", gemini_model="ge", anthropic_model="an",
        custom_llm_base_url="http://localhost:11434/v1", custom_llm_api_key="", custom_llm_model="llama3.1",
    )
    llm = build_llm(st)
    assert llm.chain == ["custom"]             # only custom configured → it's the whole chain
