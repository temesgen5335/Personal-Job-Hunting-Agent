"""The /profile endpoints: auth, round-trip, validation, and PII discipline.

Uses the same TestClient pattern as test_api.py, plus JOBAGENT_PROFILE_PATH /
JOBAGENT_CV_PATH pointed at tmp_path so the suite never touches the real data/.
"""

import pytest
from fastapi.testclient import TestClient

from jobagent.api import create_app
from jobagent.config import Settings
from jobagent.core.schemas import JobPosting, Match, Source
from jobagent.store import Store


class FakeLLM:
    chain = ["fake"]

    def complete(self, system, user, json_mode=False):
        return "TAILORED" if not json_mode else '{"score": 0.9, "rationale": "x", "gaps": []}'


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = str(tmp_path / "api.db")
    monkeypatch.setenv("JOBAGENT_DB_PATH", db)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test-pw")
    monkeypatch.setenv("JOBAGENT_MASTER_KEY", "")
    monkeypatch.setenv("JOBAGENT_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("JOBAGENT_CV_PATH", str(tmp_path / "cv_master.md"))

    s = Store(db)
    s.init_schema()
    jid = s.upsert_job(JobPosting(source=Source.remoteok, title="Senior Rust Engineer",
                                  company="Acme", is_remote=True, location="Remote",
                                  description="Rust, distributed systems"))
    s.upsert_match(Match(job_id=jid, score=0.5, rationale="x"))
    s.close()

    # No injected profile/cv → the app loads them fresh from the (tmp) overlay, which
    # is exactly the production path this feature adds.
    app = create_app(settings=Settings(_env_file=None), llm=FakeLLM(),
                     mailer=lambda *a, **k: None)
    c = TestClient(app)
    tok = c.post("/auth/login", json={"password": "test-pw"}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


def _anon(client):
    return TestClient(client.app)


# --- auth ---------------------------------------------------------------------------

def test_reading_the_profile_requires_auth_because_it_is_personal_data(client):
    """Unlike /config's non-secret read, /profile returns name, email, phone, CV — so
    even GET is gated."""
    anon = _anon(client)
    assert anon.get("/profile").status_code in (401, 403)
    assert anon.get("/profile/cv").status_code in (401, 403)
    assert anon.request("PUT", "/profile", json={}).status_code in (401, 403)


# --- round-trip ---------------------------------------------------------------------

def test_put_then_get_returns_what_was_saved(client):
    r = client.put("/profile", json={"profile": {
        "name": "Ada Lovelace", "target_roles": ["AI Engineer", "Rust Engineer"],
        "core_skills": ["Rust", "Python"], "seniority": "senior"}})
    assert r.status_code == 200

    got = client.get("/profile").json()
    assert got["profile"]["name"] == "Ada Lovelace"
    assert got["profile"]["target_roles"] == ["AI Engineer", "Rust Engineer"]
    assert got["profile"]["seniority"] == "senior"


def test_a_new_app_instance_sees_the_persisted_profile(client, tmp_path):
    """Persistence across process boundaries: a second app built over the same overlay
    path reads what the first one saved. Proves it is on disk, not in memory."""
    client.put("/profile", json={"profile": {"name": "Grace Hopper"}})

    fresh = TestClient(create_app(settings=Settings(_env_file=None), llm=FakeLLM()))
    tok = fresh.post("/auth/login", json={"password": "test-pw"}).json()["token"]
    fresh.headers.update({"Authorization": f"Bearer {tok}"})
    assert fresh.get("/profile").json()["profile"]["name"] == "Grace Hopper"


def test_saving_watchlist_and_sources_and_cv_together(client):
    r = client.put("/profile", json={
        "watchlist": {"greenhouse": ["stripe", "airbnb"], "lever": ["netflix"]},
        "sources": {"telegram": False},
        "cv_master": "# Ada\n\nRust, distributed systems, 10 years.",
    })
    assert r.status_code == 200
    got = client.get("/profile").json()
    assert got["watchlist"]["greenhouse"] == ["stripe", "airbnb"]
    assert got["sources"]["telegram"] is False
    assert got["cv"]["present"] and got["cv"]["chars"] > 0
    assert "distributed systems" in client.get("/profile/cv").json()["cv_master"]


def test_one_tab_save_does_not_clobber_another(client):
    client.put("/profile", json={"profile": {"name": "Ada"}})
    client.put("/profile", json={"watchlist": {"greenhouse": ["stripe"]}})
    got = client.get("/profile").json()
    assert got["profile"]["name"] == "Ada"                 # survived the watchlist save
    assert got["watchlist"]["greenhouse"] == ["stripe"]


def test_saving_one_profile_field_does_not_blank_the_others(client):
    """The clobber a browser caught: a partial profile PUT must persist only the keys
    sent, not every field's default. Saving the Search tab once blanked name/email
    because model_dump() emitted name="" over the real value in the lower layers.
    """
    client.put("/profile", json={"profile": {"name": "Ada", "email": "ada@x.dev"}})

    # A later save of unrelated fields (the Search tab) sends no name/email at all.
    client.put("/profile", json={"profile": {"target_roles": ["Rust Engineer"],
                                             "skill_weights": {"Rust": 2.5}}})

    got = client.get("/profile").json()["profile"]
    assert got["name"] == "Ada", "a partial save blanked the name"
    assert got["email"] == "ada@x.dev", "a partial save blanked the email"
    assert got["target_roles"] == ["Rust Engineer"]
    assert got["skill_weights"] == {"Rust": 2.5}

    # And the persisted overlay must not carry empty defaults for untouched keys.
    import json
    import os
    overlay = json.loads((open(os.environ["JOBAGENT_PROFILE_PATH"]).read()))
    assert "seniority" not in overlay["profile"] or overlay["profile"].get("seniority")


# --- validation ---------------------------------------------------------------------

def test_a_malformed_section_is_a_422_not_a_500(client):
    # skill_weights must be a dict of floats; a string is caught at the boundary.
    r = client.put("/profile", json={"profile": {"skill_weights": "not a dict"}})
    assert r.status_code == 422
    assert "Invalid profile" in r.json()["detail"]


def test_an_unknown_source_toggle_is_rejected(client):
    r = client.put("/profile", json={"sources": {"telegram": "maybe"}})
    assert r.status_code == 422


# --- usage: the saved profile changes what the API does -----------------------------

def test_a_saved_cv_unblocks_apply_prepare(client):
    """/apply/prepare 400s with no CV and succeeds once one is saved — end to end
    proof that the stored CV is the one the apply flow reads."""
    jid = client.get("/jobs").json()["jobs"][0]["id"]

    no_cv = client.post("/apply/prepare", json={"job_id": jid})
    assert no_cv.status_code == 400 and "CV" in no_cv.json()["detail"]

    client.put("/profile", json={"cv_master": "# Ada\n\nRust engineer, 10 years."})
    with_cv = client.post("/apply/prepare", json={"job_id": jid})
    assert with_cv.status_code == 200
    assert with_cv.json()["application_id"]


def test_the_committed_config_still_carries_no_real_identity(client):
    """The feature must not have reintroduced hardcoded PII: saving a profile writes to
    the gitignored overlay, never to the tracked preferences.toml (R22)."""
    import pathlib
    import re
    client.put("/profile", json={"profile": {"name": "Ada Lovelace",
                                              "email": "ada@example.dev"}})
    # `preferences.toml` is gitignored since v3.2.0; the committed one is the
    # template, and that is what must never acquire an identity.
    tracked = pathlib.Path("config/preferences.example.toml").read_text()
    assert "Ada Lovelace" not in tracked and "ada@example.dev" not in tracked
    emails = re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", tracked)
    assert emails == ["you@example.com"], f"non-placeholder email leaked: {emails}"
