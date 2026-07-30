"""Triage (dismiss / snooze / note): the storage behind the dashboard's queue.

The queue is "strong matches with no live decision" — so decisions must persist,
snoozes must lapse on their own, and undo must not eat notes.
"""

from datetime import datetime, timedelta, timezone

from jobagent.core.schemas import JobPosting, Match, Source
from jobagent.store import Store


def _store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.init_schema()
    return s


def _job(store, title, score=0.9):
    jid = store.upsert_job(JobPosting(source=Source.remoteok, title=title, company=title,
                                      is_remote=True, location="Remote"))
    store.upsert_match(Match(job_id=jid, score=score))
    return jid


def test_dismiss_persists_and_leaves_the_queue(tmp_path):
    s = _store(tmp_path)
    jid = _job(s, "AI Engineer")
    assert s.stats()["queue"] == 1
    s.set_triage(jid, state="dismissed")
    assert s.get_triage(jid)["state"] == "dismissed"
    assert s.stats()["queue"] == 0
    row = s.get_matches(limit=5)[0]
    assert row["triage_state"] == "dismissed"     # still listed, marked gone
    s.close()


def test_snooze_lapses_back_to_live(tmp_path):
    s = _store(tmp_path)
    jid = _job(s, "AI Engineer")
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    s.set_triage(jid, state="snoozed", snoozed_until=past)
    # Expired snooze reads as live everywhere — nothing re-arms it manually.
    assert s.get_triage(jid)["state"] is None
    assert s.stats()["queue"] == 1
    assert s.get_matches(limit=5)[0]["triage_state"] is None
    s.close()


def test_active_snooze_hides_from_queue(tmp_path):
    s = _store(tmp_path)
    jid = _job(s, "AI Engineer")
    future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    s.set_triage(jid, state="snoozed", snoozed_until=future)
    assert s.stats()["queue"] == 0
    assert s.get_matches(limit=5)[0]["triage_state"] == "snoozed"
    s.close()


def test_note_alone_keeps_the_job_live(tmp_path):
    s = _store(tmp_path)
    jid = _job(s, "AI Engineer")
    s.set_triage(jid, note="ask about equity")
    assert s.stats()["queue"] == 1                 # annotated ≠ decided
    row = s.get_matches(limit=5)[0]
    assert row["triage_state"] is None and row["triage_note"] == "ask about equity"
    s.close()


def test_noting_a_dismissed_job_keeps_the_dismissal_and_vice_versa(tmp_path):
    s = _store(tmp_path)
    jid = _job(s, "AI Engineer")
    s.set_triage(jid, state="dismissed")
    s.set_triage(jid, state="dismissed", note="not this company again")
    t = s.get_triage(jid)
    assert t["state"] == "dismissed" and t["note"] == "not this company again"
    s.close()


def test_undo_restores_the_queue_but_not_at_the_cost_of_the_note(tmp_path):
    s = _store(tmp_path)
    jid = _job(s, "AI Engineer")
    s.set_triage(jid, state="dismissed", note="maybe later")
    s.clear_triage(jid)
    assert s.stats()["queue"] == 1
    assert s.get_triage(jid)["note"] == "maybe later"   # undo ≠ forget
    # A bare dismissal clears to no row at all.
    jid2 = _job(s, "Backend Engineer")
    s.set_triage(jid2, state="dismissed")
    s.clear_triage(jid2)
    assert s.get_triage(jid2) is None
    s.close()


def test_weak_matches_never_count_toward_the_queue(tmp_path):
    s = _store(tmp_path)
    _job(s, "AI Engineer", score=0.9)
    _job(s, "Mediocre Fit", score=0.5)
    assert s.stats()["queue"] == 1
    s.close()


# --- API surface -------------------------------------------------------------------

def test_triage_endpoint_roundtrip(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from jobagent.api import create_app
    from jobagent.config import Settings
    from jobagent.preferences import Profile

    db = str(tmp_path / "api.db")
    monkeypatch.setenv("JOBAGENT_DB_PATH", db)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    monkeypatch.setenv("JOBAGENT_MASTER_KEY", "")
    s = Store(db)
    s.init_schema()
    jid = _job(s, "AI Engineer")
    s.close()

    c = TestClient(create_app(settings=Settings(_env_file=None), profile=Profile(name="T"),
                              llm=None, cv_master="x", mailer=lambda *a, **k: None))
    c.headers["Authorization"] = f"Bearer {c.post('/auth/login', json={'password': 'pw'}).json()['token']}"

    assert c.post(f"/triage/{jid}", json={"action": "snooze", "days": 3}).json()["state"] == "snoozed"
    assert c.get("/stats").json()["queue"] == 0

    r = c.post(f"/triage/{jid}", json={"action": "note", "note": "call them"}).json()
    assert r["state"] == "snoozed" and r["note"] == "call them"   # note keeps the snooze

    assert c.post(f"/triage/{jid}", json={"action": "clear"}).json()["state"] is None
    assert c.get("/stats").json()["queue"] == 1

    assert c.post(f"/triage/{jid}", json={"action": "bogus"}).status_code == 400
    assert c.post("/triage/nope", json={"action": "dismiss"}).status_code == 404
    # Jobs listing carries the triage columns.
    c.post(f"/triage/{jid}", json={"action": "dismiss"})
    row = c.get("/jobs").json()["jobs"][0]
    assert row["triage_state"] == "dismissed"
