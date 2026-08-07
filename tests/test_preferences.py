"""The real config/preferences.toml loads and is well-formed."""

from jobagent.preferences import load_preferences


def test_preferences_load():
    """The *tracked* config alone yields a usable search profile.

    `local_path` points at a file that cannot exist, so the gitignored identity overlay
    is excluded by construction. This test previously asserted the real name, which only
    the overlay supplies — so it passed on the author's machine and failed in CI, where
    there is no overlay. Identity is covered by the overlay tests below; this one must
    only assert what every clone of the repo gets.
    """
    prefs = load_preferences(local_path="config/no_such_overlay.toml")
    assert "AI Engineer" in prefs.profile.target_roles
    assert "remote" in prefs.profile.must_haves
    # Watchlist populated with verified slugs across all three ATS platforms.
    assert "anthropic" in prefs.watchlist.greenhouse
    assert "openai" in prefs.watchlist.ashby
    assert len(prefs.watchlist.lever) >= 1


def test_no_test_depends_on_the_gitignored_identity_overlay():
    """The class of bug, not just the instance.

    A test that reads `config/preferences.toml` without pinning `local_path` silently
    picks up whatever identity overlay the developer happens to have, which is how the
    stale assertion above stayed green locally for weeks while CI was the only thing
    that could see it.
    """
    import pathlib
    import re

    offenders = []
    for path in pathlib.Path("tests").glob("*.py"):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"load_preferences\(\s*\)", line):
                offenders.append(f"{path.name}:{lineno}")
    assert offenders == [], (
        "these tests load the real preferences file with the gitignored overlay "
        f"participating, so they are environment-dependent: {offenders}")


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
