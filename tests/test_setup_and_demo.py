"""First-run tooling: the setup wizard and the demo seeder.

Both write files, so both are dangerous to get wrong — a wizard that clobbers a tuned
`.env`, or a seeder that mixes fake postings into a real store, destroys work that
cannot be recovered. These tests are mostly about what must NOT happen.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from jobagent.setup_wizard import (
    Answers,
    env_updates,
    merge_env,
    next_steps,
    parse_env,
    profile_overlay,
    split_list,
)
from jobagent.store import Store

ROOT = Path(__file__).resolve().parent.parent


# --- the wizard must never clobber -------------------------------------------

def test_merge_env_preserves_unmanaged_keys_and_comments():
    """The single most annoying thing a setup script can do is rewrite .env from
    scratch and silently drop the SMTP block someone spent an evening on."""
    existing = (
        "# my notes\n"
        "SMTP_HOST=mail.example.com\n"
        "SMTP_PASSWORD=hunter2\n"
        "\n"
        "DASHBOARD_PASSWORD=old\n"
    )
    out = merge_env(existing, {"DASHBOARD_PASSWORD": "new", "JOBAGENT_MASTER_KEY": "k"})
    parsed = parse_env(out)

    assert parsed["SMTP_HOST"] == "mail.example.com"    # untouched
    assert parsed["SMTP_PASSWORD"] == "hunter2"         # untouched
    assert parsed["DASHBOARD_PASSWORD"] == "new"        # updated in place
    assert parsed["JOBAGENT_MASTER_KEY"] == "k"         # appended
    assert "# my notes" in out                          # comments survive


def test_merge_env_ignores_blank_updates():
    """A skipped prompt returns "". Writing it would shadow a real value with an
    explicit empty one — worse than not asking."""
    out = merge_env("DASHBOARD_PASSWORD=keepme\n", {"DASHBOARD_PASSWORD": ""})
    assert parse_env(out)["DASHBOARD_PASSWORD"] == "keepme"


def test_env_updates_omits_a_provider_without_its_key():
    """Setting LLM_PROVIDER with no key produces a chain whose primary always fails."""
    assert "LLM_PROVIDER" not in env_updates(Answers(llm_provider="groq"), master_key="k")
    got = env_updates(Answers(llm_provider="groq", llm_api_key="x"), master_key="k")
    assert got["LLM_PROVIDER"] == "groq" and got["GROQ_API_KEY"] == "x"


def test_split_list_drops_empties():
    """An empty skill matches nothing and reads as a bug in the matcher."""
    assert split_list("Python, , Go,  ,SQL") == ["Python", "Go", "SQL"]
    assert split_list("") == []


# --- the overlay it writes ----------------------------------------------------

def test_profile_overlay_merges_rather_than_replaces():
    """Re-running setup must not wipe fields the dashboard set — it writes the same
    layer Settings does."""
    existing = {"profile": {"phone": "+123", "name": "Old"}, "watchlist": {"lever": ["x"]}}
    out = profile_overlay(Answers(name="New", core_skills=["Go"]), existing)

    assert out["profile"]["name"] == "New"        # answered → updated
    assert out["profile"]["phone"] == "+123"      # unanswered → preserved
    assert out["watchlist"] == {"lever": ["x"]}   # untouched section survives


def test_named_skills_start_weighted_above_default():
    """The skills you name are what you want to be hired for. Leaving them at 1.0
    means a posting mentioning Docker scores like one built on your differentiators."""
    out = profile_overlay(Answers(core_skills=["Rust", "Postgres"]))
    assert out["profile"]["skill_weights"] == {"Rust": 2.0, "Postgres": 2.0}


def test_remote_only_sets_both_the_mode_and_the_must_have():
    """work_mode alone is a preference; must_haves is what the matcher penalises on."""
    out = profile_overlay(Answers(remote_only=True))
    assert out["profile"]["work_mode"] == "remote"
    assert "remote" in out["profile"]["must_haves"]


def test_next_steps_lead_with_something_that_works_without_credentials():
    steps = next_steps(Answers(), has_llm=False, has_telegram=False)
    assert "make pipeline" in steps[0]
    assert any("Optional" in s and "LLM" in s for s in steps)


# --- the demo seeder must never touch a real store ---------------------------

def test_seed_demo_refuses_a_store_that_already_has_jobs(tmp_path):
    """The guard that matters: demo rows mixed into a real store are indistinguishable
    afterwards without reading every description."""
    db = tmp_path / "real.db"
    from jobagent.core.schemas import JobPosting, Source

    s = Store(str(db))
    s.init_schema()
    s.upsert_job(JobPosting(source=Source.remoteok, title="Real Job", company="Real Co"))
    s.close()

    result = subprocess.run(
        [sys.executable, "scripts/seed_demo.py", "--db", str(db)],
        cwd=ROOT, capture_output=True, text=True)
    assert result.returncode != 0
    assert "already has" in result.stdout
    s = Store(str(db))
    assert s.count_jobs() == 1, "the seeder wrote into a populated store"
    s.close()


def test_seed_demo_produces_a_store_the_dashboard_can_render(tmp_path):
    """An empty demo is no better than an empty store — it needs strong matches, a
    triaged row, applications, and an ingest event, or pages still render empty."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from seed_demo import seed

    stats = seed(str(tmp_path / "demo.db"), jobs=40)
    assert stats["total_jobs"] == 40
    assert stats["strong_matches"] > 0, "no strong matches → the triage queue is empty"
    assert stats["queue"] > 0
    assert stats["total_apps"] == 3, "no applications → the funnel and tracker are empty"
    assert stats["health"]["last_ingest"], "no ingest event → dashboard reports never run"
    assert stats["health"]["is_stale"] is False


def test_every_demo_posting_is_marked_as_fake(tmp_path):
    """A demo row that looks real is worse than no demo at all."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from seed_demo import MARK, seed

    db = tmp_path / "demo.db"
    seed(str(db), jobs=12)
    s = Store(str(db))
    try:
        assert all(MARK in (j["description"] or "") for j in s.get_jobs())
    finally:
        s.close()


def test_seeding_is_deterministic(tmp_path):
    """Two people following the README should see the same screenshots."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from seed_demo import seed

    a = seed(str(tmp_path / "a.db"), jobs=20)
    b = seed(str(tmp_path / "b.db"), jobs=20)
    assert a["strong_matches"] == b["strong_matches"]
    assert a["queue"] == b["queue"]


# --- containers ---------------------------------------------------------------

@pytest.mark.parametrize("path", ["Dockerfile", "dashboard/Dockerfile", "compose.yml",
                                  ".dockerignore"])
def test_container_files_exist(path):
    assert (ROOT / path).exists(), f"{path} missing"


def test_dockerignore_excludes_every_secret_and_personal_path():
    """A .dockerignore miss bakes credentials or a CV into a published image layer,
    where deleting the file afterwards does not remove it."""
    text = (ROOT / ".dockerignore").read_text()
    for pattern in (".env", "data/", "*.session", "config/preferences.toml",
                    "config/cv_master.md", "docs/*.pdf"):
        assert pattern in text, f".dockerignore does not exclude {pattern}"


def test_compose_binds_host_ports_to_loopback_only():
    """GET routes are unauthenticated (SECURITY.md), so a compose file that published
    0.0.0.0:8077 would expose the operator's application history to their whole
    network the moment they ran `docker compose up`."""
    text = (ROOT / "compose.yml").read_text()
    published = [ln.strip() for ln in text.splitlines()
                 if ln.strip().startswith("- \"") and ":" in ln]
    assert published, "no port mappings found — did the format change?"
    for line in published:
        assert "127.0.0.1:" in line, f"port published beyond loopback: {line}"
