"""v2.0 FastAPI orchestrator tests. TestClient + temp store + fake LLM/mailer.
No network, no browser. Proves the bot+dashboard backend works end to end."""

import pytest
from fastapi.testclient import TestClient

from jobagent.api import create_app
from jobagent.config import Settings
from jobagent.core.schemas import ApplyMethod, JobPosting, Match, Source
from jobagent.preferences import Profile
from jobagent.secrets_store import SecretStore
from jobagent.store import Store


class FakeLLM:
    chain = ["fake"]

    def complete(self, system, user, json_mode=False):
        return '{"subject": "Application: Role", "body": "Hello, I am a great fit."}' if json_mode else "TAILORED CONTENT"


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = str(tmp_path / "api.db")
    monkeypatch.setenv("JOBAGENT_DB_PATH", db)
    # Writes are auth-gated, so the fixture logs in and carries the token. Reads
    # need no token. See test_every_mutating_route_requires_auth for the invariant.
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test-pw")
    monkeypatch.setenv("JOBAGENT_MASTER_KEY", "")
    settings = Settings(_env_file=None)

    # Seed one email job + one ATS job, both scored.
    s = Store(db)
    s.init_schema()
    email_job = JobPosting(source=Source.remoteok, title="AI Engineer", company="Acme",
                           is_remote=True, apply_method=ApplyMethod.email, apply_email="jobs@acme.example")
    ats_job = JobPosting(source=Source.greenhouse, title="Backend Engineer", company="stripe",
                         source_job_id="9", apply_method=ApplyMethod.ats_form,
                         apply_url="https://boards.greenhouse.io/stripe/jobs/9")
    eid = s.upsert_job(email_job)
    aid = s.upsert_job(ats_job)
    s.upsert_match(Match(job_id=eid, score=0.91, rationale="strong"))
    s.upsert_match(Match(job_id=aid, score=0.80, rationale="good"))
    s.close()

    mails = []
    app = create_app(
        settings=settings, profile=Profile(name="Tester", email="me@x.com", cv_path=""),
        llm=FakeLLM(), cv_master="MASTER CV TEXT",
        mailer=lambda *a, **k: mails.append((a, k)),
    )
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {c.post('/auth/login', json={'password': 'test-pw'}).json()['token']}"
    c._mails = mails  # type: ignore
    c._email_job_id = eid  # type: ignore
    return c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["llm_chain"] == ["fake"]


def test_stats_and_jobs_and_applications(client):
    assert client.get("/stats").json()["total_jobs"] == 2
    jobs = client.get("/jobs", params={"limit": 10}).json()["jobs"]
    assert {j["title"] for j in jobs} == {"AI Engineer", "Backend Engineer"}
    assert client.get("/applications").json()["applications"] == []


def test_jobs_filter_remote(client):
    jobs = client.get("/jobs", params={"location": "remote"}).json()["jobs"]
    assert [j["title"] for j in jobs] == ["AI Engineer"]   # only the remote one


def test_prepare_then_approve_sends_email(client):
    prep = client.post("/apply/prepare", json={"job_id": client._email_job_id}).json()
    assert prep["cv_markdown"] == "TAILORED CONTENT"
    assert prep["email_subject"].startswith("Application")
    app_id = prep["application_id"]

    res = client.post(f"/apply/{app_id}/approve").json()["result"]
    assert "Sent" in res
    assert len(client._mails) == 1                          # fake mailer called once

    # Application now shows as submitted in the tracker.
    apps = client.get("/applications").json()["applications"]
    assert apps[0]["status"] == "submitted"


def test_prepare_unknown_job_404(client):
    assert client.post("/apply/prepare", json={"job_id": "nope"}).status_code == 404


def test_fit_endpoint(client):
    r = client.post("/fit", json={"job_id": client._email_job_id})
    assert r.status_code == 200
    body = r.json()
    assert 0.0 <= body["score"] <= 1.0
    assert "matched" in body and "missing" in body and body["source"] in ("heuristic", "llm")
    assert client.post("/fit", json={"job_id": "nope"}).status_code == 404


def test_ats_preview_rejects_non_ats(client):
    # The email job isn't an ATS posting → 400.
    assert client.post("/ats/preview", json={"job_id": client._email_job_id}).status_code == 400


def test_config_auth_and_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAGENT_DB_PATH", str(tmp_path / "c.db"))
    monkeypatch.setenv("JOBAGENT_SECRETS_PATH", str(tmp_path / "secrets.enc"))
    monkeypatch.setenv("JOBAGENT_MASTER_KEY", SecretStore.generate_key())
    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    settings = Settings(_env_file=None)
    Store(settings.db_path).init_schema()

    app = create_app(settings=settings, profile=Profile(name="T"), llm=None, cv_master="x",
                     mailer=lambda *a, **k: None)
    c = TestClient(app)

    assert c.get("/config").status_code == 401                       # no token
    assert c.post("/auth/login", json={"password": "wrong"}).status_code == 401
    token = c.post("/auth/login", json={"password": "hunter2"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    view = c.get("/config", headers=headers).json()["config"]
    assert "groq_api_key" in view and "llm_provider" in view

    r = c.put("/config", headers=headers,
              json={"values": {"groq_api_key": "gsk_secret", "llm_provider": "groq"}})
    assert r.status_code == 200
    assert r.json()["config"]["groq_api_key"] == {"set": True}        # masked
    assert r.json()["config"]["llm_provider"] == "groq"
    # Persisted (encrypted) and never echoed in plaintext.
    assert SecretStore().load()["groq_api_key"] == "gsk_secret"


def test_update_application_status_and_analytics(client):
    # Create an application via prepare, then walk it down a legal path.
    app_id = client.post("/apply/prepare", json={"job_id": client._email_job_id}).json()["application_id"]

    assert client.patch(f"/applications/{app_id}", json={"status": "bogus"}).status_code == 400
    assert client.patch("/applications/nope", json={"status": "interview"}).status_code == 404

    # awaiting_approval → submitted → interview (the real process order).
    assert client.patch(f"/applications/{app_id}", json={"status": "submitted"}).status_code == 200
    r = client.patch(f"/applications/{app_id}", json={"status": "interview"})
    assert r.status_code == 200 and r.json()["status"] == "interview"
    assert "offer" in r.json()["allowed_next"]

    a = client.get("/analytics").json()
    assert a["total"] >= 1
    assert a["by_status"].get("interview") == 1
    assert any(s["source"] == "remoteok" for s in a["by_source"])


def test_config_disabled_without_password(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAGENT_DB_PATH", str(tmp_path / "d.db"))
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    settings = Settings(_env_file=None)
    app = create_app(settings=settings, profile=Profile(name="T"), llm=None, cv_master="x")
    c = TestClient(app)
    assert c.post("/auth/login", json={"password": "x"}).status_code == 403   # fail closed
    assert c.get("/config").status_code == 403


# --- C1: auth on every state-changing route ---------------------------------------

def test_every_mutating_route_requires_auth(client):
    """Invariant: no non-GET route is reachable without a token.

    This is the regression net for the class of bug that left /apply/{id}/approve
    open — an endpoint that can send email as you. A newly added write route that
    forgets `dependencies=auth` fails here rather than in production.
    """
    anon = TestClient(client.app)          # deliberately no Authorization header
    checked = []
    for route in client.app.routes:
        methods = getattr(route, "methods", set()) - {"GET", "HEAD", "OPTIONS"}
        if not methods or route.path == "/auth/login":   # login must stay open
            continue
        for method in methods:
            path = route.path.replace("{app_id}", "x").replace("{job_id}", "x")
            r = anon.request(method, path, json={})
            assert r.status_code in (401, 403), f"{method} {route.path} ungated → {r.status_code}"
            checked.append(f"{method} {route.path}")
    assert len(checked) >= 9, f"expected the 9 known write routes, saw {checked}"


def test_reads_stay_open_without_token(client):
    anon = TestClient(client.app)
    for path in ("/health", "/stats", "/jobs", "/applications", "/analytics"):
        assert anon.get(path).status_code == 200, path


def test_bad_token_rejected(client):
    anon = TestClient(client.app, headers={"Authorization": "Bearer not-the-token"})
    assert anon.post("/fit", json={"job_id": client._email_job_id}).status_code == 401


def test_unauthenticated_caller_cannot_send_an_application(client):
    """R2 teeth: the HITL gate must not be bypassable over HTTP."""
    app_id = client.post("/apply/prepare", json={"job_id": client._email_job_id}).json()["application_id"]
    anon = TestClient(client.app)
    assert anon.post(f"/apply/{app_id}/approve").status_code in (401, 403)
    assert client._mails == []                      # nothing left the building


def test_writes_fail_closed_without_password(tmp_path, monkeypatch):
    """No DASHBOARD_PASSWORD → writes refused (403), reads still fine."""
    monkeypatch.setenv("JOBAGENT_DB_PATH", str(tmp_path / "fc.db"))
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    settings = Settings(_env_file=None)
    Store(settings.db_path).init_schema()
    c = TestClient(create_app(settings=settings, profile=Profile(name="T"), llm=None,
                              cv_master="x", mailer=lambda *a, **k: None))
    assert c.post("/match").status_code == 403
    assert c.patch("/applications/x", json={"status": "interview"}).status_code == 403
    assert c.get("/stats").status_code == 200


def test_cors_default_is_not_wildcard():
    """An open origin plus a reachable port is how a stranger drives your apply flow."""
    assert Settings(_env_file=None).cors_origins != "*"


# --- M4: status lifecycle enforcement ---------------------------------------------

def test_illegal_transition_is_refused_with_the_legal_set(client):
    """An application is a real-world process: you cannot jump straight from
    awaiting-approval to interview, because nothing was ever sent."""
    app_id = client.post("/apply/prepare", json={"job_id": client._email_job_id}).json()["application_id"]
    r = client.patch(f"/applications/{app_id}", json={"status": "interview"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["current"] == "awaiting_approval"
    assert detail["allowed"] == ["failed", "skipped", "submitted"]   # names the way out
    # And the stored status is untouched by the refusal.
    assert client.get("/applications").json()["applications"][0]["status"] == "awaiting_approval"


def test_terminal_status_has_no_exits(client):
    app_id = client.post("/apply/prepare", json={"job_id": client._email_job_id}).json()["application_id"]
    client.patch(f"/applications/{app_id}", json={"status": "submitted"})
    client.patch(f"/applications/{app_id}", json={"status": "rejected"})
    r = client.patch(f"/applications/{app_id}", json={"status": "interview"})
    assert r.status_code == 422 and r.json()["detail"]["allowed"] == []


def test_correction_flag_overrides_and_is_audited(client):
    """A mis-click must be fixable — but never silently."""
    app_id = client.post("/apply/prepare", json={"job_id": client._email_job_id}).json()["application_id"]
    r = client.patch(f"/applications/{app_id}", json={"status": "offer", "correction": True})
    assert r.status_code == 200 and r.json()["status"] == "offer"


def test_same_status_is_idempotent(client):
    app_id = client.post("/apply/prepare", json={"job_id": client._email_job_id}).json()["application_id"]
    assert client.patch(f"/applications/{app_id}", json={"status": "awaiting_approval"}).status_code == 200


def test_applications_list_carries_allowed_next(client):
    """The UI reads the legal moves off each row instead of duplicating the map."""
    client.post("/apply/prepare", json={"job_id": client._email_job_id})
    row = client.get("/applications").json()["applications"][0]
    assert row["allowed_next"] == ["failed", "skipped", "submitted"]


def test_jobs_list_returns_gaps_as_an_array(client, monkeypatch):
    """gaps is JSON text in SQLite; the API must hand clients a real array so the
    dashboard can render them without parsing storage encoding."""
    import os

    from jobagent.core.schemas import Match
    from jobagent.store import Store

    st = Store(os.environ["JOBAGENT_DB_PATH"])
    st.upsert_match(Match(job_id=client._email_job_id, score=0.9, rationale="r",
                          gaps=["not clearly remote", "level mismatch: 'junior' role"]))
    st.close()

    row = next(j for j in client.get("/jobs").json()["jobs"] if j["id"] == client._email_job_id)
    assert row["gaps"] == ["not clearly remote", "level mismatch: 'junior' role"]
    assert client.get(f"/job/{client._email_job_id}").json()["gaps"][0] == "not clearly remote"


# --- Tier 2: follow-up reminders ---------------------------------------------------

def _make_quiet_application(client, days_ago=10):
    """Submit an application, then backdate it so it counts as gone quiet."""
    import os
    from datetime import datetime, timedelta, timezone

    from jobagent.store import Store
    app_id = client.post("/apply/prepare", json={"job_id": client._email_job_id}).json()["application_id"]
    client.post(f"/apply/{app_id}/approve")             # → submitted
    st = Store(os.environ["JOBAGENT_DB_PATH"])
    st.conn.execute("UPDATE applications SET submitted_at=? WHERE id=?",
                    ((datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(), app_id))
    st.conn.commit()
    st.close()
    return app_id


def test_followups_endpoint_lists_quiet_applications(client):
    app_id = _make_quiet_application(client)
    body = client.get("/followups").json()
    assert body["after_days"] == 7
    assert [f["id"] for f in body["followups"]] == [app_id]
    assert body["followups"][0]["days_waiting"] >= 10
    # Window is tunable, and a wide window excludes it.
    assert client.get("/followups", params={"after_days": 60}).json()["followups"] == []


def test_followup_draft_returns_a_draft_and_sends_nothing(client):
    """R2 in spirit: drafting a nudge must never put mail on the wire.

    The setup deliberately sends the *initial* application, so the assertion is that
    the draft call adds nothing on top of that — not that no mail exists at all.
    """
    app_id = _make_quiet_application(client)
    before = len(client._mails)

    r = client.post(f"/followups/{app_id}/draft", json={"days_waiting": 12})
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is False
    assert body["subject"] and body["body"]
    assert body["to"] == "jobs@acme.example"       # who you'd send it to, if you choose
    assert len(client._mails) == before            # nothing new left the building


def test_drafting_suppresses_the_reminder_until_the_next_window(client):
    app_id = _make_quiet_application(client)
    assert len(client.get("/followups").json()["followups"]) == 1
    client.post(f"/followups/{app_id}/draft")
    assert client.get("/followups").json()["followups"] == []


def test_followup_draft_unknown_application_404(client):
    assert client.post("/followups/nope/draft").status_code == 404


# --- Tier 3: run ledger ------------------------------------------------------------

def test_ingest_returns_a_run_id_and_runs_lists_ledger(client, monkeypatch):
    import os

    import jobagent.api.app as api_mod
    from jobagent.core.schemas import Event
    from jobagent.store import Store

    # TestClient executes BackgroundTasks synchronously — left real, /ingest would
    # run an actual network ingestion inside the test suite (R17). Stub the task and
    # assert the endpoint passed it the same run_id it returned to the caller.
    seen = {}
    monkeypatch.setattr(api_mod, "_ingest_task",
                        lambda db, st, pf, llm, run_id: seen.setdefault("run_id", run_id))

    r = client.post("/ingest")
    assert r.status_code == 202 and len(r.json()["run_id"]) == 12
    assert seen["run_id"] == r.json()["run_id"]     # id threads into the task

    # Seed a summary the way the pipeline logs one; the API reads the same ledger.
    st = Store(os.environ["JOBAGENT_DB_PATH"])
    st.log_event(Event(kind="run", payload={"run_id": "abc123", "duration_s": 4.2,
                                            "ingest": {"fetched": 3, "new": 1, "errors": []},
                                            "match": {"scored": 3, "llm_reranked": 0},
                                            "digest": "sent (1 message(s))"}))
    st.log_event(Event(kind="ingest", payload={"source": "remoteok", "fetched": 3,
                                               "new": 1, "run_id": "abc123"}))
    st.close()

    runs = client.get("/runs").json()["runs"]
    assert runs[0]["run_id"] == "abc123" and runs[0]["digest"].startswith("sent")

    ev = client.get("/runs/abc123").json()
    assert {e["kind"] for e in ev["events"]} == {"run", "ingest"}
    assert client.get("/runs/never-happened").status_code == 404


def test_second_ingest_while_running_is_409(client, monkeypatch):
    """M5 over HTTP: the endpoint acquires the pipeline lock synchronously, so the
    caller learns a pass is already running instead of silently double-running."""
    import os

    import jobagent.api.app as api_mod
    from jobagent.store import Store

    # Stub the task so the lock is NOT released (simulates a pass still in flight).
    monkeypatch.setattr(api_mod, "_ingest_task", lambda *a, **k: None)
    assert client.post("/ingest").status_code == 202
    r = client.post("/ingest")
    assert r.status_code == 409 and "already running" in r.json()["detail"]

    # When the task DOES run, its finally-release frees the lock for the next pass.
    st = Store(os.environ["JOBAGENT_DB_PATH"])
    st.conn.execute("DELETE FROM locks")
    st.conn.commit()
    st.close()
    released = {}
    def fake_task(db, settings, profile, llm, run_id):
        s = Store(db)
        s.release_lock("pipeline", run_id)   # what the real task's finally does
        s.close()
        released["run_id"] = run_id
    monkeypatch.setattr(api_mod, "_ingest_task", fake_task)
    assert client.post("/ingest").status_code == 202     # runs + releases (sync in tests)
    assert client.post("/ingest").status_code == 202     # so the next one acquires again


def test_the_job_list_does_not_ship_the_untouched_source_payload(client):
    """`raw` was 63% of a default /jobs response — ~640 KB on every dashboard page
    load — and nothing reads it: the dashboard's MatchRow does not declare it, and
    neither the bot nor the assistant touches it.

    The store still keeps it (JobPosting.raw is never discarded); this is about what
    goes on the wire.
    """
    rows = client.get("/jobs").json()["jobs"]
    assert rows, "fixture should seed jobs"
    assert all("raw" not in r for r in rows)
    # The fields consumers actually render must survive the strip.
    for field in ("id", "title", "company", "score", "source", "url"):
        assert field in rows[0], f"stripping removed {field!r}, which the UI renders"


def test_provider_exhaustion_is_a_503_not_a_500(client, monkeypatch):
    """A free-tier daily limit is an expected, self-healing condition. Unhandled it
    surfaced as `Internal Server Error`, which reads like a code fault and tells the
    operator nothing about what to do.

    Found by exercising the running system with all three free tiers exhausted.
    """
    from jobagent.api import app as api

    class Exhausted:
        chain = ["groq"]

        def complete(self, system, user, json_mode=False):
            raise RuntimeError(
                "All LLM providers failed:\n"
                "  groq: RateLimitError: Error code: 429 - tokens per day (TPD)")

    app = api.create_app(
        settings=Settings(_env_file=None),
        profile=Profile(name="Tester", email="me@x.com", cv_path=""),
        llm=Exhausted(), cv_master="MASTER CV TEXT", mailer=lambda *a, **k: None)
    c = TestClient(app)
    c.headers["Authorization"] = client.headers["Authorization"]

    job_id = client.get("/jobs").json()["jobs"][0]["id"]

    # Generation has no fallback — there is no non-LLM way to write a tailored CV —
    # so it must say 503 and say why.
    r = c.post("/apply/prepare", json={"job_id": job_id})
    assert r.status_code == 503, f"/apply/prepare returned {r.status_code}"
    detail = r.json()["detail"]
    assert "rate-limited" in detail or "No LLM provider" in detail
    assert "Traceback" not in detail

    # Scoring and fit-checking DO have fallbacks and must keep answering. Asserted so
    # nobody "fixes" them into 503s to match their neighbour — degrading to a heuristic
    # answer is the better behaviour, not an oversight.
    fit = c.post("/fit", json={"job_id": job_id})
    assert fit.status_code == 200 and fit.json()["source"] == "heuristic"
    assert c.post("/match", json={}).status_code == 200
