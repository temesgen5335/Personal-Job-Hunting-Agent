"""The Operator: one thread owns the Store, the governed toolbox and the audit trail.

R15 says never share a Store across threads; the SDK dispatches tool calls on worker
threads. Rather than trust every call site to remember, the Operator makes concurrent
access impossible by construction.
"""

import threading

import pytest

from jobagent.assistant.operator_tools import OperatorDeps
from jobagent.config import Settings
from jobagent.core.schemas import JobPosting
from jobagent.mcp.operator import Operator
from jobagent.store.db import Store


def _settings(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAGENT_DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOBAGENT_PROFILE_PATH", str(tmp_path / "profile.json"))
    monkeypatch.setenv("JOBAGENT_CV_PATH", str(tmp_path / "cv.md"))
    return Settings(_env_file=None)


def _deps(tmp_path):
    return OperatorDeps(db_path=str(tmp_path / "t.db"), local_path=str(tmp_path / "none.toml"),
                        overlay_path=str(tmp_path / "profile.json"), cv_loader=lambda: "",
                        llm_factory=lambda: None, env_path=str(tmp_path / ".env"),
                        spawn=lambda fn: fn())


@pytest.fixture
def op(tmp_path, monkeypatch):
    operator = Operator(_settings(tmp_path, monkeypatch), deps=_deps(tmp_path))
    operator.start()
    yield operator
    operator.close()


def test_every_governed_call_runs_on_the_one_owning_thread(op):
    owner = op.run(lambda: threading.current_thread().name)
    assert owner.startswith("operator") and owner != threading.current_thread().name
    result = op.execute("pipeline_health", {}, ask=None)
    assert not result.is_error and "jobs=0" in result.content
    # The Store was created on, and is only ever touched from, the owning thread.
    assert op.run(lambda: threading.current_thread().name) == owner


def test_reentrant_calls_from_the_owning_thread_do_not_deadlock(op):
    assert op.run(lambda: op.run(lambda: 42)) == 42


def test_the_session_opens_with_a_note_and_closes_onto_the_run_ledger(tmp_path, monkeypatch):
    operator = Operator(_settings(tmp_path, monkeypatch), deps=_deps(tmp_path))
    operator.start()
    run_id = operator.run_id
    operator.execute("pipeline_health", {}, ask=None)
    operator.close(summary="done")
    operator.close(summary="done")          # idempotent

    store = Store(str(tmp_path / "t.db"))
    try:
        kinds = [e["kind"] for e in store.events_for_run(run_id)]
        assert kinds[0] == "agent_session_open"
        assert kinds.count("run") == 1 and kinds[-1] == "run"
        assert {"tool_intent", "tool_decision", "tool_result"} <= set(kinds)
        sessions = store.list_runs(kind_detail="agent_session")
        assert sessions and sessions[0]["surface"] == "agent" and sessions[0]["admin"] is False
        assert store.list_runs() == []      # never mixed into the pipeline ledger
    finally:
        store.close()


def test_admin_tools_are_hidden_unless_the_operator_opted_in(tmp_path, monkeypatch):
    plain = Operator(_settings(tmp_path, monkeypatch), deps=_deps(tmp_path))
    plain.start()
    try:
        names = {s.name for s in plain.specs()}
        assert "apply_config_change" not in names and "rollback_config" not in names
        assert "propose_config_change" in names       # READ stays: compute, then hand over
        refused = plain.execute("apply_config_change",
                                {"field": "ingest_max_age_days", "value": "30"}, ask=lambda *_: True)
        assert refused.is_error and "not available" in refused.content
    finally:
        plain.close()

    admin = Operator(_settings(tmp_path, monkeypatch), admin=True, deps=_deps(tmp_path))
    admin.start()
    try:
        assert "apply_config_change" in {s.name for s in admin.specs()}
        assert admin.may_confirm_admin()
    finally:
        admin.close()


def test_the_card_is_computed_from_arguments_not_prose(op):
    a = op.card("triage", {"job_id": "abc", "state": "dismissed"})
    b = op.card("triage", {"job_id": "abc", "state": "snoozed"})
    assert a != b and "state: dismissed" in a and a == op.card("triage", {"job_id": "abc", "state": "dismissed"})


def test_a_session_grant_is_visible_to_the_renderer(op):
    store = Store(str(op.settings.db_path))
    try:
        job_id = store.upsert_job(JobPosting(title="X", company="Y", source="remoteok", url="http://x/1"))
    finally:
        store.close()
    assert not op.is_granted("triage")
    ok = op.execute("triage", {"job_id": job_id, "state": "dismissed"}, ask=lambda *_: True)
    assert not ok.is_error
    assert op.is_granted("triage")
    assert op.policy_for("triage").confirm.value == "session"
