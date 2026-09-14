"""Status moves go through one function, whichever surface asks (R23)."""

import pytest

from jobagent.core.schemas import Application, JobPosting
from jobagent.lifecycle import (
    IllegalTransition, NoSuchApplication, UnknownStatus, VALID_STATUSES, transition,
)
from jobagent.store.db import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.init_schema()
    return s


def _app(store, status="matched") -> str:
    job_id = store.upsert_job(JobPosting(title="AI Engineer", company="Acme",
                                         source="remoteok", url="http://x/1"))
    return store.create_application(Application(job_id=job_id, status=status))


def test_a_legal_move_updates_the_row_and_names_what_comes_next(store):
    app_id = _app(store, "matched")
    t = transition(store, app_id, "drafting", source="test")
    assert (t.previous, t.status, t.corrected) == ("matched", "drafting", False)
    assert "awaiting_approval" in t.allowed_next
    assert store.get_application(app_id)["status"] == "drafting"
    assert t.job_id == store.get_application(app_id)["job_id"]


def test_an_illegal_move_is_refused_and_names_the_legal_set(store):
    app_id = _app(store, "matched")
    with pytest.raises(IllegalTransition) as exc:
        transition(store, app_id, "offer")
    assert exc.value.current == "matched" and exc.value.target == "offer"
    assert exc.value.allowed == ["drafting", "skipped"]
    assert store.get_application(app_id)["status"] == "matched"       # nothing changed


def test_a_correction_bypasses_the_map_and_is_audited_with_its_reason(store):
    app_id = _app(store, "matched")
    t = transition(store, app_id, "offer", correction=True, source="agent", reason="mis-click")
    assert t.corrected and t.status == "offer"
    events = [e for e in store.conn.execute("SELECT kind, payload FROM events")]
    kinds = [e["kind"] for e in events]
    assert "status_correction" in kinds
    payload = next(e["payload"] for e in events if e["kind"] == "status_correction")
    assert '"source": "agent"' in payload and '"reason": "mis-click"' in payload


def test_a_legal_move_with_correction_set_is_not_recorded_as_a_correction(store):
    app_id = _app(store, "matched")
    t = transition(store, app_id, "drafting", correction=True)
    assert not t.corrected
    kinds = [e["kind"] for e in store.conn.execute("SELECT kind FROM events")]
    assert "status_correction" not in kinds


def test_same_to_same_is_an_idempotent_no_op(store):
    app_id = _app(store, "submitted")
    assert transition(store, app_id, "submitted").status == "submitted"


def test_unknown_application_and_unknown_status_are_distinct_errors(store):
    with pytest.raises(NoSuchApplication):
        transition(store, "nope", "drafting")
    app_id = _app(store)
    with pytest.raises(UnknownStatus):
        transition(store, app_id, "hired")
    assert "matched" in VALID_STATUSES and "hired" not in VALID_STATUSES
