"""Exposure controls: optional read authentication and per-class rate limits.

Both default to the historical behaviour, so these tests are as much about the OFF
state as the ON one — a security feature that quietly changes the default breaks
every existing install.
"""

import pytest
from fastapi.testclient import TestClient

from jobagent.api import create_app
from jobagent.api.ratelimit import RateLimiter, client_key
from jobagent.config import Settings
from jobagent.core.schemas import JobPosting, Match, Source
from jobagent.preferences import Profile
from jobagent.store import Store

PW = "test-pw"
READ_ROUTES = ("/stats", "/jobs", "/applications", "/analytics", "/followups",
               "/sources", "/runs")


def _app(tmp_path, monkeypatch, **env):
    db = str(tmp_path / "ra.db")
    monkeypatch.setenv("JOBAGENT_DB_PATH", db)
    monkeypatch.setenv("DASHBOARD_PASSWORD", PW)
    monkeypatch.setenv("JOBAGENT_MASTER_KEY", "")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    s = Store(db)
    s.init_schema()
    jid = s.upsert_job(JobPosting(source=Source.remoteok, title="Role", company="Co"))
    s.upsert_match(Match(job_id=jid, score=0.9))
    s.close()
    return create_app(settings=Settings(_env_file=None), profile=Profile(name="T"),
                      llm=None, cv_master="x", mailer=lambda *a, **k: None)


def _token(client) -> str:
    return client.post("/auth/login", json={"password": PW}).json()["token"]


# --- default posture is unchanged --------------------------------------------

def test_reads_stay_open_by_default(tmp_path, monkeypatch):
    """The historical behaviour, and correct on the default 127.0.0.1 bind. Turning
    this on silently would break every existing install's dashboard."""
    c = TestClient(_app(tmp_path, monkeypatch))
    for path in READ_ROUTES:
        assert c.get(path).status_code == 200, path


# --- opt-in read auth ---------------------------------------------------------

def test_every_read_route_is_gated_when_the_flag_is_on(tmp_path, monkeypatch):
    """The point of the feature: /applications and /followups reveal where you
    applied, what was rejected, and where you are interviewing."""
    app = _app(tmp_path, monkeypatch, JOBAGENT_REQUIRE_AUTH_READS="true")
    anon = TestClient(app)
    for path in READ_ROUTES:
        assert anon.get(path).status_code in (401, 403), f"{path} still open"

    authed = TestClient(app)
    authed.headers["Authorization"] = f"Bearer {_token(authed)}"
    for path in READ_ROUTES:
        assert authed.get(path).status_code == 200, f"{path} rejects a valid token"


def test_no_get_route_is_left_ungated_when_reads_are_authenticated(tmp_path, monkeypatch):
    """Route-table invariant, the same shape as the write-auth one. A GET added next
    year without `dependencies=read_auth` fails here rather than leaking quietly.

    /health is the deliberate exception: it is a liveness probe (the Docker
    HEALTHCHECK calls it) and says nothing about the job search.
    """
    app = _app(tmp_path, monkeypatch, JOBAGENT_REQUIRE_AUTH_READS="true")
    anon = TestClient(app)
    checked = []
    for route in app.routes:
        if "GET" not in getattr(route, "methods", set()):
            continue
        path = route.path
        if path in ("/health", "/openapi.json", "/docs", "/redoc",
                    "/docs/oauth2-redirect"):
            continue
        r = anon.get(path.replace("{job_id}", "x").replace("{run_id}", "x")
                         .replace("{app_id}", "x"))
        assert r.status_code in (401, 403, 404), f"GET {path} ungated → {r.status_code}"
        checked.append(path)
    assert len(checked) >= 9, f"expected the known read routes, saw {checked}"


def test_health_stays_open_so_probes_keep_working(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, JOBAGENT_REQUIRE_AUTH_READS="true")
    assert TestClient(app).get("/health").status_code == 200


def test_read_auth_without_a_password_refuses_to_start(tmp_path, monkeypatch):
    """Reads gated + no password = no token exists, so every page 403s forever. That
    reads as "the app is broken", so refuse at startup instead."""
    monkeypatch.setenv("JOBAGENT_DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.setenv("JOBAGENT_REQUIRE_AUTH_READS", "true")
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="nothing would be readable"):
        create_app(settings=Settings(_env_file=None), profile=Profile(name="T"),
                   llm=None, cv_master="x")


# --- rate limits --------------------------------------------------------------

def test_the_bucket_refills_continuously_rather_than_in_windows():
    """A fixed window lets a caller spend the whole allowance in its last second and
    again in the next window's first — twice the intended rate at the worst moment."""
    now = [0.0]
    limiter = RateLimiter(per_hour=2, clock=lambda: now[0])
    assert limiter.allow("a")[0] and limiter.allow("a")[0]
    allowed, retry = limiter.allow("a")
    assert not allowed and retry > 0

    now[0] += 1800.0                       # half an hour → exactly one token back
    assert limiter.allow("a")[0]
    assert not limiter.allow("a")[0]


def test_limits_are_per_client():
    limiter = RateLimiter(per_hour=1)
    assert limiter.allow("1.2.3.4")[0]
    assert not limiter.allow("1.2.3.4")[0]
    assert limiter.allow("5.6.7.8")[0], "one client exhausted another's budget"


def test_zero_disables_a_class():
    limiter = RateLimiter(per_hour=0)
    assert all(limiter.allow("a")[0] for _ in range(50))


def test_client_key_uses_only_the_first_forwarded_hop():
    """The rest of X-Forwarded-For is attacker-controlled; trusting it would let a
    caller mint a fresh bucket per request."""
    class Req:
        headers = {"x-forwarded-for": "9.9.9.9, 1.1.1.1, 2.2.2.2"}
        client = None

    assert client_key(Req()) == "9.9.9.9"


def test_an_exhausted_class_returns_429_with_retry_after(tmp_path, monkeypatch):
    """A caller that cannot tell "slow down" from "broken" retries harder.

    Uses /triage rather than /ingest: POST /ingest schedules a real ingestion pass, so
    exercising it here would fetch from live job boards (R17 — no test touches the
    network). The limiter is class-agnostic; what matters is that a class trips.
    """
    app = _app(tmp_path, monkeypatch, JOBAGENT_RATE_LIMIT_WRITE="2")
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {_token(c)}"
    job_id = c.get("/jobs").json()["jobs"][0]["id"]
    body = {"action": "note", "note": "x"}

    seen = [c.post(f"/triage/{job_id}", json=body).status_code for _ in range(4)]
    assert 429 in seen, f"write limit of 2 never tripped: {seen}"
    last = c.post(f"/triage/{job_id}", json=body)
    assert last.status_code == 429
    assert int(last.headers["Retry-After"]) >= 1
    assert "JOBAGENT_RATE_LIMIT_WRITE" in last.json()["detail"], (
        "the error must name the knob that changes it"
    )


def test_the_ingest_class_is_limited_without_touching_the_network(tmp_path, monkeypatch):
    """`/ingest` gets its own, tighter class because it makes outbound requests. The
    background task is stubbed — the assertion is about the gate, not the pass."""
    import jobagent.api.app as api_mod

    app = _app(tmp_path, monkeypatch, JOBAGENT_RATE_LIMIT_INGEST="1")
    monkeypatch.setattr(api_mod, "_ingest_task",
                        lambda db_path, settings, profile, llm, run_id: None)
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {_token(c)}"

    first = c.post("/ingest")
    assert first.status_code in (202, 409)
    assert c.post("/ingest").status_code == 429


def test_limits_do_not_apply_to_reads(tmp_path, monkeypatch):
    """Reads are cheap and the dashboard makes several per page load. Limiting them
    would throttle normal use, which is how a rate limiter gets switched off."""
    app = _app(tmp_path, monkeypatch, JOBAGENT_RATE_LIMIT_WRITE="1")
    c = TestClient(app)
    assert all(c.get("/stats").status_code == 200 for _ in range(30))


def test_rate_limiting_can_be_turned_off_entirely(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, JOBAGENT_RATE_LIMIT_ENABLED="false",
               JOBAGENT_RATE_LIMIT_WRITE="1")
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {_token(c)}"
    job_id = c.get("/jobs").json()["jobs"][0]["id"]
    codes = [c.post(f"/triage/{job_id}", json={"action": "note", "note": "x"}).status_code
             for _ in range(3)]
    assert codes.count(429) == 0
