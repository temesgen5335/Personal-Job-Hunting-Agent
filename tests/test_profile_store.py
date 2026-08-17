"""The profile/preferences store: persistence, precedence, recall, and USAGE.

The requirement was aggressive coverage of "correct settings preservation, setting,
recall and usage" — so these do not stop at "the file was written". They prove the
saved profile is what the next request reads, that the three layers stack in the right
order, that a clone with no overlay still works, and — the part that actually matters —
that an edited profile reaches the code that matches jobs and drafts messages.

Every test is hermetic: JOBAGENT_PROFILE_PATH / JOBAGENT_CV_PATH point into tmp_path,
so nothing here can read or write the developer's real data/ (R17).
"""

import json

import pytest

from jobagent.preferences import (
    Profile,
    load_cv_master,
    load_overlay,
    load_preferences,
    save_cv_master,
    save_overlay,
)


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """Fully isolated layers: an in-tmp committed base and a missing local.toml, plus
    env-pinned overlay and CV. Every layer is under our control, so no test here can
    read the developer's real preferences.local.toml — the leak that has bitten twice.

    Returns (overlay_path, cv_path, load) where `load()` reads through all three
    isolated layers, so tests never make the env-dependent no-argument call.
    """
    base = tmp_path / "preferences.toml"
    base.write_text("[profile]\n")                       # empty, known base
    overlay = tmp_path / "profile.json"
    cv = tmp_path / "cv_master.md"
    monkeypatch.setenv("JOBAGENT_PROFILE_PATH", str(overlay))
    monkeypatch.setenv("JOBAGENT_CV_PATH", str(cv))

    def load():
        return load_preferences(path=str(base),
                                local_path=str(tmp_path / "none.toml"),
                                overlay_path=str(overlay))

    return overlay, cv, load


# --- setting + recall ---------------------------------------------------------------

def test_a_saved_profile_is_what_the_next_load_reads(paths):
    overlay, _, load = paths
    save_overlay({"profile": {"name": "Ada Lovelace", "target_roles": ["AI Engineer"]}},
                 overlay_path=str(overlay))
    prefs = load()
    assert prefs.profile.name == "Ada Lovelace"
    assert prefs.profile.target_roles == ["AI Engineer"]


def test_saving_one_section_does_not_wipe_another(paths):
    overlay, _, load = paths
    save_overlay({"profile": {"name": "Ada"}}, overlay_path=str(overlay))
    save_overlay({"watchlist": {"greenhouse": ["stripe"]}}, overlay_path=str(overlay))
    save_overlay({"profile": {"seniority": "senior"}}, overlay_path=str(overlay))
    prefs = load()
    assert prefs.profile.name == "Ada"                    # survived the watchlist save
    assert prefs.profile.seniority == "senior"            # merged, did not replace
    assert prefs.watchlist.greenhouse == ["stripe"]


def test_a_null_section_clears_it_back_to_the_lower_layers(tmp_path):
    # Fully isolated layers, so "the lower layers" is a base we control — not whatever
    # preferences.local.toml the developer happens to have (the leak that broke the
    # first version of this test, and test_preferences_load before it).
    base = tmp_path / "preferences.toml"
    base.write_text('[profile]\nname = "base name"\n')
    overlay = tmp_path / "profile.json"
    kw = dict(path=str(base), local_path=str(tmp_path / "none.toml"),
              overlay_path=str(overlay))

    save_overlay({"profile": {"name": "Temp"}}, overlay_path=str(overlay))
    assert load_preferences(**kw).profile.name == "Temp"
    save_overlay({"profile": None}, overlay_path=str(overlay))
    assert load_preferences(**kw).profile.name == "base name"   # fell back to the base


def test_the_overlay_is_valid_json_on_disk(paths):
    overlay, _, _ = paths
    save_overlay({"profile": {"name": "Ada", "core_skills": ["Python", "Rust"]}},
                 overlay_path=str(overlay))
    on_disk = json.loads(overlay.read_text())
    assert on_disk["profile"]["core_skills"] == ["Python", "Rust"]


def test_load_overlay_returns_only_the_writable_layer_not_the_merge(paths):
    overlay, _, _ = paths
    save_overlay({"profile": {"name": "Ada"}}, overlay_path=str(overlay))
    assert load_overlay(overlay_path=str(overlay)) == {"profile": {"name": "Ada"}}


# --- layer precedence ---------------------------------------------------------------

def test_the_writable_overlay_wins_over_the_committed_base(tmp_path, monkeypatch):
    base = tmp_path / "preferences.toml"
    base.write_text('[profile]\nname = "Your Name"\ntarget_roles = ["SWE"]\n')
    overlay = tmp_path / "profile.json"
    overlay.write_text(json.dumps({"profile": {"name": "Real Person"}}))

    prefs = load_preferences(path=str(base), local_path=str(tmp_path / "none.toml"),
                             overlay_path=str(overlay))
    assert prefs.profile.name == "Real Person"       # overlay wins
    assert prefs.profile.target_roles == ["SWE"]      # base survives where overlay is silent


def test_overlay_beats_the_legacy_local_toml(tmp_path):
    base = tmp_path / "preferences.toml"
    base.write_text('[profile]\nname = "placeholder"\n')
    (tmp_path / "preferences.local.toml").write_text('[profile]\nname = "from local toml"\n')
    overlay = tmp_path / "profile.json"
    overlay.write_text(json.dumps({"profile": {"name": "from data overlay"}}))

    prefs = load_preferences(path=str(base), overlay_path=str(overlay))
    assert prefs.profile.name == "from data overlay"


def test_a_clone_with_no_overlay_still_yields_a_usable_profile(tmp_path):
    # The REAL committed base with no overlay and no local.toml — a fresh clone. Pinned
    # explicitly (not the bare call) so the developer's identity overlay cannot leak in.
    prefs = load_preferences(path="config/preferences.toml",
                             local_path=str(tmp_path / "none.toml"),
                             overlay_path=str(tmp_path / "none.json"))
    assert isinstance(prefs.profile, Profile)
    assert prefs.profile.target_roles          # committed placeholders ship some roles


def test_a_corrupt_overlay_does_not_brick_loading(paths):
    overlay, _, load = paths
    overlay.write_text("{ this is not json")
    prefs = load()                             # must not raise
    assert isinstance(prefs.profile, Profile)


# --- CV round-trip ------------------------------------------------------------------

def test_cv_round_trips_through_the_data_copy(paths):
    _, cv, _ = paths
    save_cv_master("# Ada Lovelace\n\nFirst programmer. Python, analytical engines.",
                   cv_path=str(cv))
    assert "analytical engines" in load_cv_master(cv_path=str(cv))


def test_an_explicit_cv_path_is_authoritative_no_legacy_fallback(tmp_path):
    """An explicit override never falls back to config/cv_master.md — that is what
    makes a test see an empty CV, not the developer's real one."""
    data_cv = tmp_path / "data_cv.md"
    assert load_cv_master(cv_path=str(data_cv)) == ""      # nothing there yet
    save_cv_master("DATA CV WINS", cv_path=str(data_cv))
    assert load_cv_master(cv_path=str(data_cv)) == "DATA CV WINS"


# --- USAGE: the edited profile must reach the code that uses it ---------------------

def test_an_edited_profile_changes_how_jobs_are_scored(paths):
    """The point of the whole feature: a saved preference is not just stored, it is
    used. Rank the same posting under two different saved profiles and the score moves.
    """
    from jobagent.matching.heuristic import heuristic_score

    job = {"title": "Senior Rust Engineer", "company": "Acme",
           "description": "Rust, systems programming, distributed systems",
           "location": "Remote", "tags": ["rust", "systems"]}

    overlay, _, load = paths
    save_overlay({"profile": {"target_roles": ["Rust Engineer"], "seniority": "senior",
                              "core_skills": ["Rust", "systems programming"],
                              "skill_weights": {"Rust": 2.0}}}, overlay_path=str(overlay))
    strong = heuristic_score(job, load().profile)

    save_overlay({"profile": {"target_roles": ["Frontend Engineer"], "seniority": "junior",
                              "core_skills": ["React", "CSS"], "skill_weights": {}}},
                 overlay_path=str(overlay))
    weak = heuristic_score(job, load().profile)

    assert strong > weak, f"profile edit did not change scoring ({strong} vs {weak})"


def test_a_saved_cv_is_what_a_generator_is_grounded_on(paths):
    """R1 grounding: the tailored email may only make claims present in the CV. So the
    CV the generator sees must be the one just saved — not a stale or empty default."""
    from jobagent.apply.generators import email_prompt

    _, cvp, _ = paths
    save_cv_master("Ada Lovelace — 10 years of Rust and distributed systems.",
                   cv_path=str(cvp))
    cv = load_cv_master(cv_path=str(cvp))
    _, user = email_prompt("Ada", {"title": "Rust Engineer", "company": "Acme"}, cv)
    assert "distributed systems" in user     # the saved CV reached the prompt
