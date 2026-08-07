"""Encrypted secret store + settings overlay + custom LLM provider (v2.1)."""

import types


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


# --- the .env fallback, and the hermeticity it must not break ----------------------

def test_a_key_set_only_in_dotenv_reaches_the_store(tmp_path, monkeypatch):
    """The bug this fallback exists for.

    Everything else reads config through pydantic-settings, which loads `.env` into a
    Settings object but does not export it to os.environ. This module read os.environ
    alone, so a key set in `.env` — the location .env.example documents — never arrived:
    reads appeared fine (an absent store needs no crypto) while every *write* failed
    with "not set". That silently broke the Settings page, the assistant's config tool
    and rollback for anyone following the documented setup.
    """
    from jobagent.secrets_store import SecretStore

    key = SecretStore.generate_key()
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"JOBAGENT_MASTER_KEY={key}\n")
    monkeypatch.delenv("JOBAGENT_MASTER_KEY", raising=False)
    monkeypatch.delenv("JOBAGENT_SECRETS_PATH", raising=False)

    store = SecretStore(path=str(tmp_path / "s.enc"))
    store.update({"groq_model": "llama-3.3-70b-versatile"})     # the path that failed
    assert store.load()["groq_model"] == "llama-3.3-70b-versatile"


def test_an_explicitly_empty_env_var_is_not_backfilled_from_dotenv(tmp_path, monkeypatch):
    """Presence, not truthiness.

    A test that sets the variable to "" means *explicitly no key*. Backfilling it from
    `.env` would make the suite depend on whatever the developer has on their own
    machine — the same non-hermetic failure that once made test_preferences_load pass
    locally and fail in CI.
    """
    from jobagent.secrets_store import _from_env_or_dotenv

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("JOBAGENT_MASTER_KEY=should-never-be-used\n")

    monkeypatch.setenv("JOBAGENT_MASTER_KEY", "")
    assert _from_env_or_dotenv("JOBAGENT_MASTER_KEY") == ""

    monkeypatch.delenv("JOBAGENT_MASTER_KEY")
    assert _from_env_or_dotenv("JOBAGENT_MASTER_KEY") == "should-never-be-used"


def test_the_env_var_still_wins_over_dotenv(tmp_path, monkeypatch):
    from jobagent.secrets_store import _from_env_or_dotenv

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("JOBAGENT_MASTER_KEY=from-dotenv\n")
    monkeypatch.setenv("JOBAGENT_MASTER_KEY", "from-environ")
    assert _from_env_or_dotenv("JOBAGENT_MASTER_KEY") == "from-environ"
