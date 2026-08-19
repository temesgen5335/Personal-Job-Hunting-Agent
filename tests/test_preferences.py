"""Preference loading: what a clone gets, and what only a configured install gets."""

import tomllib
from pathlib import Path

from jobagent.preferences import EXAMPLE_PATH, load_preferences


def test_preferences_load():
    """The *tracked* config alone yields a usable search profile.

    `local_path` points at a file that cannot exist, so the gitignored identity overlay
    is excluded by construction.

    This test has now been broken twice by the same mistake in two different places, so
    it is worth stating the rule it exists to enforce: **assert only what a clone gets.**
    v1 asserted the author's real name, which only the gitignored overlay supplies. v2
    asserted the author's target roles and curated ATS watchlist, which only the
    gitignored `preferences.toml` supplies. Both passed on the author's machine — where
    those files exist — and failed in CI, which is the only place the property is
    actually observable.

    So it no longer hardcodes values at all: it compares what loads against what the
    committed template declares. That cannot drift, because editing the template updates
    both sides at once.
    """
    # ALL THREE layers pinned. `path` matters as much as the other two: on a developer
    # machine `config/preferences.toml` exists (gitignored, real), so leaving the base
    # layer at its default made this test read the author's profile — the third variant
    # of the same mistake. A hermetic test names every file it depends on.
    prefs = load_preferences(
        path=EXAMPLE_PATH,
        local_path="config/no_such_overlay.toml",
        overlay_path="config/no_such_overlay.json",
    )
    template = tomllib.loads(Path(EXAMPLE_PATH).read_text())

    assert prefs.profile.target_roles == template["profile"]["target_roles"]
    assert prefs.profile.core_skills == template["profile"]["core_skills"]
    assert prefs.watchlist.greenhouse == template["watchlist"]["greenhouse"]
    assert prefs.watchlist.ashby == template["watchlist"]["ashby"]

    # And the shape a clone needs to actually run: something to search for, and at
    # least one employer on each ATS so the adapters have work to do.
    assert prefs.profile.target_roles and prefs.profile.core_skills
    assert prefs.profile.must_haves
    assert all(len(v) >= 1 for v in (prefs.watchlist.greenhouse, prefs.watchlist.lever,
                                     prefs.watchlist.ashby))


def test_no_test_depends_on_the_gitignored_identity_overlay():
    """The class of bug, not just the instance.

    Every layer must be pinned, not just the overlay. The first version of this guard
    only caught a bare `load_preferences()`, so `load_preferences(local_path=...)` — which
    pins the overlay but leaves the BASE at its default — sailed through and read the
    developer's gitignored `config/preferences.toml`. That is the same failure the guard
    exists to prevent, one argument along, and CI was again the only place it showed.

    So the rule is now the whole rule: a test that calls `load_preferences` names `path`.
    """
    import pathlib
    import re

    call = re.compile(r"load_preferences\(")
    offenders = []
    for file in sorted(pathlib.Path("tests").glob("*.py")):
        lines = file.read_text().splitlines()
        for lineno, line in enumerate(lines, 1):
            if not call.search(line):
                continue
            # The call may wrap across lines; look at the whole argument list.
            window = " ".join(lines[lineno - 1:lineno + 4])
            args = window[window.index("load_preferences(") + len("load_preferences("):]
            first = args.lstrip()
            # `(?<!\w)` matters: a plain `"path=" in args` also matches the `path=`
            # inside `local_path=` and `overlay_path=`, which made the first version of
            # this check pass every offender it was written to catch.
            pinned = (
                re.search(r"(?<!\w)path\s*=", args)     # named explicitly
                or "**" in first[:4]                    # forwarded from a pinned dict
                or bool(first) and not re.match(r"[A-Za-z_]\w*\s*=", first)
            )                                           # ...or given positionally
            if not pinned:
                offenders.append(f"{file.name}:{lineno}: {line.strip()[:60]}")
    assert offenders == [], (
        "these tests leave the preferences BASE at its default, so they read whatever "
        f"gitignored config the developer happens to have: {offenders}")


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
    # The committed file is the TEMPLATE — `preferences.toml` is gitignored since
    # v3.2.0 and does not exist in a clone, so reading it here crashed CI.
    text = pathlib.Path("config/preferences.example.toml").read_text()
    emails = re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    assert emails == ["you@example.com"], f"non-placeholder email in tracked config: {emails}"
    # A real phone number; the placeholder is all zeros after the country code.
    phones = [m for m in re.findall(r"\+\d{6,}", text) if m != "+10000000000"]
    assert phones == [], f"real phone number in tracked config: {phones}"
