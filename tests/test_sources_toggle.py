"""Per-source on/off toggles drive which adapters the registry builds."""

import types

from jobagent.ingestion.registry import build_adapters
from jobagent.preferences import Preferences, Sources, Watchlist


def _settings():
    # Minimal settings stub for build_adapters (slugs/channels empty → ATS/telegram self-gate).
    return types.SimpleNamespace(
        greenhouse_slugs="", lever_slugs="", ashby_slugs="",
        telegram_api_id=None, telegram_api_hash="", telegram_channels="",
        telegram_session="data/telegram", telegram_fetch_limit=50,
    )


def test_default_sources_all_built(monkeypatch):
    monkeypatch.setattr("jobagent.ingestion.registry.load_preferences", lambda: Preferences())
    names = {a.source.value for a in build_adapters(_settings())}
    assert {"remoteok", "remotive", "greenhouse", "lever", "ashby", "telegram"} <= names


def test_disabled_sources_are_dropped(monkeypatch):
    prefs = Preferences(sources=Sources(remoteok=False, telegram=False, greenhouse=False))
    monkeypatch.setattr("jobagent.ingestion.registry.load_preferences", lambda: prefs)
    names = {a.source.value for a in build_adapters(_settings())}
    assert "remoteok" not in names
    assert "telegram" not in names
    assert "greenhouse" not in names
    assert {"remotive", "lever", "ashby"} <= names   # the rest still built


def test_sources_is_enabled_default_true_for_unknown():
    assert Sources().is_enabled("remoteok") is True
    assert Sources(remoteok=False).is_enabled("remoteok") is False
    assert Sources().is_enabled("aggregator") is False  # off by default
