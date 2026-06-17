"""The real config/preferences.toml loads and is well-formed."""

from jobagent.preferences import load_preferences


def test_preferences_load():
    prefs = load_preferences()
    assert prefs.profile.name == "Temesgen Gebreabzgi"
    assert "AI Engineer" in prefs.profile.target_roles
    assert "remote" in prefs.profile.must_haves
    # Watchlist populated with verified slugs across all three ATS platforms.
    assert "anthropic" in prefs.watchlist.greenhouse
    assert "openai" in prefs.watchlist.ashby
    assert len(prefs.watchlist.lever) >= 1


def test_missing_file_returns_empty_defaults():
    prefs = load_preferences("config/does_not_exist.toml")
    assert prefs.profile.name == ""
    assert prefs.watchlist.greenhouse == []
