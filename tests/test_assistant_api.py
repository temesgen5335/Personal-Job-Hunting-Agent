"""The assistant's HTTP surface, especially the two-phase confirmation.

HTTP cannot block waiting for a human, so the flow differs from the CLI: the model's
turn completes *without* the write, and the approval is a separate request. The tests
that matter here are the ones proving that split does not weaken the guarantee.
"""

import pytest
from fastapi.testclient import TestClient

from jobagent.api import create_app
from jobagent.api.assistant_routes import PendingRegistry
from jobagent.config import Settings
from jobagent.core.schemas import JobPosting, Match, Source
from jobagent.store import Store


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = str(tmp_path / "api.db")
    monkeypatch.setenv("JOBAGENT_DB_PATH", db)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test-pw")
    monkeypatch.setenv("JOBAGENT_MASTER_KEY", "")
    # No provider keys: /assistant/ask must fail cleanly rather than reach the network.
    for key in ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY", "QWEN_API_KEY", "CUSTOM_LLM_BASE_URL"):
        monkeypatch.setenv(key, "")

    s = Store(db)
    s.init_schema()
    job_id = s.upsert_job(JobPosting(source=Source.remoteok, title="AI Engineer",
                                     company="Acme", is_remote=True, location="Remote",
                                     description="python and llms"))
    s.upsert_match(Match(job_id=job_id, score=0.9, rationale="strong"))
    s.close()

    settings = Settings(_env_file=None)
    app = create_app(settings=settings)
    c = TestClient(app)
    token = c.post("/auth/login", json={"password": "test-pw"}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {token}"})
    c.job_id = job_id
    return c


# --- the endpoints ------------------------------------------------------------------

def test_ask_requires_a_question(client):
    assert client.post("/assistant/ask", json={"question": "   "}).status_code == 422


def test_an_overlong_question_is_refused_rather_than_sent(client):
    r = client.post("/assistant/ask", json={"question": "x" * 5000})
    assert r.status_code == 422 and "too long" in r.json()["detail"]


def test_ask_reports_the_absence_of_a_provider_instead_of_failing_obscurely(client):
    """With no key configured this must say so, not surface a connection error from
    inside an SDK."""
    r = client.post("/assistant/ask", json={"question": "how are things?"})
    assert r.status_code == 503
    assert "provider" in r.json()["detail"].lower()


def test_sessions_are_listed_separately_from_pipeline_runs(client):
    import os

    from jobagent.core.schemas import Event

    store = Store(os.environ["JOBAGENT_DB_PATH"])
    store.log_event(Event(kind="run", payload={"run_id": "pipe1", "ingest": {"new": 1}}))
    store.log_event(Event(kind="run", payload={"run_id": "sess1",
                                               "kind_detail": "agent_session"}))
    store.close()

    sessions = client.get("/assistant/sessions").json()["sessions"]
    runs = client.get("/runs").json()["runs"]
    assert [x["run_id"] for x in sessions] == ["sess1"]
    assert [x["run_id"] for x in runs] == ["pipe1"]


# --- the confirmation split ----------------------------------------------------------

def test_a_confirmation_carries_only_a_nonce_so_there_is_nothing_to_tamper_with(client):
    """The structural upgrade HTTP allows.

    The CLI binds an approval to `sha256(args)` and checks it. Here the client never
    holds the arguments at all — they stay server-side — so confirm-then-swap has no
    field to happen in. There is nothing to substitute.
    """
    import inspect

    from jobagent.api import assistant_routes
    source = inspect.getsource(assistant_routes.register)
    confirm = source[source.index("def confirm("):]
    signature = confirm[:confirm.index(")")]
    assert "nonce" in signature
    for smell in ("args", "field", "value", "payload", "body", "req"):
        assert smell not in signature, \
            f"the confirm endpoint accepts {smell!r} from the caller; arguments must " \
            f"come only from server-side storage"


def test_an_unknown_or_reused_nonce_is_refused(client):
    assert client.post("/assistant/confirm/never-existed").status_code == 404


def test_a_pending_approval_is_single_use():
    reg = PendingRegistry()
    reg.add("n1", "apply_config_change", {"field": "ingest_max_age_days", "value": "30"},
            "card")
    assert reg.take("n1") is not None
    assert reg.take("n1") is None


def test_a_pending_approval_expires():
    clock = {"t": 0.0}
    reg = PendingRegistry(now=lambda: clock["t"])
    reg.add("n1", "triage", {"job_id": "x", "state": "dismissed"}, "card")
    clock["t"] += 10_000
    assert reg.take("n1") is None


def test_expired_approvals_are_swept_rather_than_accumulating():
    clock = {"t": 0.0}
    reg = PendingRegistry(now=lambda: clock["t"])
    for i in range(5):
        reg.add(f"n{i}", "triage", {"job_id": str(i)}, "card")
    clock["t"] += 10_000
    reg.add("fresh", "triage", {"job_id": "z"}, "card")
    assert list(reg._items) == ["fresh"]


def test_the_stored_arguments_are_what_get_executed(client):
    """The registry holds the arguments the card described, and `take` returns exactly
    those — the confirm handler has no other source for them."""
    reg = PendingRegistry()
    args = {"field": "ingest_max_age_days", "value": "30"}
    reg.add("n1", "apply_config_change", args, "ingest_max_age_days: 0 → 30")
    item = reg.take("n1")
    assert item.tool == "apply_config_change" and item.args == args
    assert "0 → 30" in item.card


def test_a_confirmed_write_runs_through_the_route_and_is_audited(client):
    """The real two-phase flow: seed an approval the way `ask` does, then POST the
    confirm endpoint and check the world actually changed.

    An earlier version of this test only exercised the registry and claimed to be end
    to end. It would have passed with the confirm handler deleted.
    """
    import os

    pending = client.app.state.assistant_pending
    pending.add("nonce-abc", "triage",
                {"job_id": client.job_id, "state": "dismissed"}, "card text")

    r = client.post("/assistant/confirm/nonce-abc")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tool"] == "triage" and body["ok"] is True

    store = Store(os.environ["JOBAGENT_DB_PATH"])
    try:
        assert store.get_triage(client.job_id)["state"] == "dismissed"
        trail = [e["kind"] for e in store.events_for_run(body["run_id"])]
        assert trail == ["tool_intent", "tool_decision", "tool_result", "run"]
    finally:
        store.close()


def test_the_same_confirmation_cannot_be_replayed(client):
    pending = client.app.state.assistant_pending
    pending.add("nonce-once", "triage",
                {"job_id": client.job_id, "state": "dismissed"}, "card")
    assert client.post("/assistant/confirm/nonce-once").status_code == 200
    assert client.post("/assistant/confirm/nonce-once").status_code == 404


def test_confirming_does_not_run_a_different_tool_than_was_approved(client):
    """The gatekeeper mints and redeems its own argument-bound nonce underneath the
    HTTP one, so the binding is enforced twice. Corrupt the stored arguments and the
    inner check refuses."""
    pending = client.app.state.assistant_pending
    pending.add("nonce-swap", "triage",
                {"job_id": client.job_id, "state": "dismissed"}, "card")
    pending._items["nonce-swap"].args["state"] = "snoozed"     # tamper server-side

    assert client.post("/assistant/confirm/nonce-swap").status_code == 409
