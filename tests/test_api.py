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


def test_config_disabled_without_password(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAGENT_DB_PATH", str(tmp_path / "d.db"))
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    settings = Settings(_env_file=None)
    app = create_app(settings=settings, profile=Profile(name="T"), llm=None, cv_master="x")
    c = TestClient(app)
    assert c.post("/auth/login", json={"password": "x"}).status_code == 403   # fail closed
    assert c.get("/config").status_code == 403
