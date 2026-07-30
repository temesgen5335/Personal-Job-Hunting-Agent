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


# --- C2: identity overlay (committed config stays PII-free) ------------------------

def test_local_overlay_wins_per_section(tmp_path):
    """preferences.local.toml supplies identity; the committed file supplies the
    search config. Merge is section-wise, so the overlay does not wipe siblings."""
    base = tmp_path / "preferences.toml"
    base.write_text(
        '[profile]\nname = "Your Name"\nemail = "you@example.com"\n'
        'target_roles = ["AI Engineer"]\ncore_skills = ["Python"]\n'
        '[watchlist]\ngreenhouse = ["stripe"]\n'
    )
    (tmp_path / "preferences.local.toml").write_text(
        '[profile]\nname = "Real Person"\nemail = "real@me.test"\n'
    )
    p = load_preferences(str(base))
    assert p.profile.name == "Real Person"                 # overlay wins
    assert p.profile.email == "real@me.test"
    assert p.profile.target_roles == ["AI Engineer"]       # base survives the merge
    assert p.profile.core_skills == ["Python"]
    assert p.watchlist.greenhouse == ["stripe"]            # untouched section


def test_absent_overlay_leaves_base_intact(tmp_path):
    base = tmp_path / "preferences.toml"
    base.write_text('[profile]\nname = "Only Base"\ntarget_roles = ["SRE"]\n')
    p = load_preferences(str(base))
    assert p.profile.name == "Only Base" and p.profile.target_roles == ["SRE"]


def test_committed_preferences_carry_no_personal_contact_details():
    """Guards the repo against re-acquiring PII: the tracked config must stay
    placeholder-only, with real identity in the gitignored overlay."""
    import pathlib
    import re
    text = pathlib.Path("config/preferences.toml").read_text()
    emails = re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    assert emails == ["you@example.com"], f"non-placeholder email in tracked config: {emails}"
    # A real phone number; the placeholder is all zeros after the country code.
    phones = [m for m in re.findall(r"\+\d{6,}", text) if m != "+10000000000"]
    assert phones == [], f"real phone number in tracked config: {phones}"
