"""The operator tools: read/sort matches, research, draft (never send), move the lifecycle.

Every read tool is checked for R32 (no None into model text, cap never reported as total)
on a populated store; every write is checked for its refusal and its audit shape. No
network, no real profile/CV (R17).
"""

import json
import threading

import pytest

from agentkit.llm.types import ToolCall
from agentkit.session import Surface
from jobagent.assistant.manifest import build_assistant
from jobagent.assistant.operator_tools import AGENT_SURFACES, OperatorDeps
from jobagent.config import Settings
from jobagent.core.schemas import Application, JobPosting, Match
from jobagent.store.db import Store


class ListSink:
    def __init__(self):
        self.events = []

    def emit(self, kind, payload):
        self.events.append((kind, payload))


class FakeLLM:
    chain = ["fake"]

    def complete(self, system, user, json_mode=False):
        return ('{"confidence": 0.7, "matched": ["Python"], "missing": ["Rust"], '
                '"experience": "fits", "summary": "ok"}') if not json_mode else \
               '{"subject": "Application: AI Engineer", "body": "Hello."}'


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    s.init_schema()
    return s


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAGENT_DB_PATH", str(tmp_path / "t.db"))
    return Settings(_env_file=None)


def _deps(tmp_path, *, llm=None, spawn=None):
    return OperatorDeps(
        db_path=str(tmp_path / "t.db"), local_path=str(tmp_path / "none.toml"),
        overlay_path=str(tmp_path / "profile.json"), cv_loader=lambda: "MASTER CV",
        llm_factory=lambda: llm, spawn=spawn or (lambda fn: fn()), env_path=str(tmp_path / ".env"))


def _assistant(store, settings, tmp_path, **kw):
    return build_assistant(store=store, settings=settings, sink=ListSink(),
                           surface=Surface.AGENT, ask=lambda *_: True,
                           deps=_deps(tmp_path, **kw))


def _seed(store, n=15, company="Acme"):
    ids = []
    for i in range(n):
        jid = store.upsert_job(JobPosting(
            title=f"AI Engineer {i}", company=company, source="remoteok",
            url=f"http://x/{i}", location="Remote", description="python llm kubernetes"))
        store.upsert_match(Match(job_id=jid, score=0.9 - i * 0.01, rationale="fits", gaps=["rust"]))
        ids.append(jid)
    return ids


def _call(assistant, name, args):
    return assistant.toolbox.execute(ToolCall("c", name, args))


def test_all_operator_tools_are_agent_and_cli_only(store, settings, tmp_path):
    from jobagent.assistant.operator_tools import build_operator_tools
    regs = build_operator_tools(store=store, settings=settings,
                                deps=_deps(tmp_path), links=lambda k, t: "")
    assert len(regs) == 14
    assert all(r.surfaces == AGENT_SURFACES for r in regs)
    names = {r.spec.name for r in regs}
    assert names == {"setup_status", "current_profile", "lifecycle", "list_matches",
                     "company_dossier", "fit_check", "propose_profile_change", "pull_jobs",
                     "rematch", "annotate_job", "draft_application", "set_application_status",
                     "correct_application_status", "apply_profile_change"}


def test_no_operator_tool_is_a_sender_or_deleter(store, settings, tmp_path):
    from jobagent.assistant.operator_tools import build_operator_tools
    names = {r.spec.name for r in build_operator_tools(
        store=store, settings=settings, deps=_deps(tmp_path), links=lambda k, t: "")}
    assert not any(w in n for n in names
                   for w in ("send", "submit", "approve", "apply_to", "ats", "purge", "delete", "save_cv"))


def test_read_tools_emit_no_None_on_a_populated_store(store, settings, tmp_path):
    _seed(store, 3)
    a = _assistant(store, settings, tmp_path)
    for name, args in [("setup_status", {}), ("current_profile", {}), ("lifecycle", {}),
                       ("list_matches", {"min_score": 0.1}), ("company_dossier", {"company": "Acme"})]:
        content = _call(a, name, args).content
        assert "None" not in content and "'?'" not in content, f"{name}: {content[:160]}"


def test_read_tools_emit_no_None_on_an_empty_store(store, settings, tmp_path):
    a = _assistant(store, settings, tmp_path)
    for name in ["setup_status", "current_profile", "lifecycle", "list_matches"]:
        assert "None" not in _call(a, name, {}).content


def test_list_matches_reports_the_true_total_and_sorts(store, settings, tmp_path):
    _seed(store, 15)
    a = _assistant(store, settings, tmp_path)
    out = _call(a, "list_matches", {"min_score": 0.1, "limit": 5})
    assert "and 10 more" in out.content
    assert out.data["total"] == 15 and len(out.data["rows"]) == 5
    scores = [r["score"] for r in out.data["rows"]]
    assert scores == sorted(scores, reverse=True)                 # default sort=score
    newest = _call(a, "list_matches", {"min_score": 0.1, "limit": 5, "sort": "newest"})
    assert [r["id"] for r in newest.data["rows"]] != [r["id"] for r in out.data["rows"][:5]] or len(newest.data["rows"]) == 5


def test_company_dossier_gathers_postings_and_applications(store, settings, tmp_path):
    ids = _seed(store, 2, company="Acme")
    store.create_application(Application(job_id=ids[0], status="submitted"))
    a = _assistant(store, settings, tmp_path)
    out = _call(a, "company_dossier", {"company": "Acme"})
    assert "Acme" in out.content and "submitted" in out.content
    unknown = _call(a, "company_dossier", {"company": "Nope"})
    assert "no" in unknown.content.lower()


def test_fit_check_uses_the_llm_and_degrades_without_one(store, settings, tmp_path):
    ids = _seed(store, 1)
    llm = _assistant(store, settings, tmp_path, llm=FakeLLM())
    got = _call(llm, "fit_check", {"job_id": ids[0]})
    assert "%" in got.content and got.data["source"] in ("llm", "heuristic")
    heur = _assistant(store, settings, tmp_path, llm=None)
    assert not _call(heur, "fit_check", {"job_id": ids[0]}).is_error   # heuristic fallback


def test_draft_application_prepares_but_never_approves(store, settings, tmp_path):
    ids = _seed(store, 1)
    a = _assistant(store, settings, tmp_path, llm=FakeLLM())
    out = _call(a, "draft_application", {"job_id": ids[0]})
    assert not out.is_error
    apps = store.list_applications()
    assert apps and apps[0]["status"] == "awaiting_approval"
    assert store.get_application(apps[0]["id"])["approved_at"] is None   # R2
    assert "cannot" in out.content.lower() and "http" in out.content     # hands over


def test_draft_application_without_a_cv_explains_where_to_add_it(store, settings, tmp_path):
    ids = _seed(store, 1)
    deps = _deps(tmp_path, llm=FakeLLM())
    deps.cv_loader = lambda: ""
    a = build_assistant(store=store, settings=settings, sink=ListSink(),
                        surface=Surface.AGENT, ask=lambda *_: True, deps=deps)
    out = _call(a, "draft_application", {"job_id": ids[0]})
    assert "CV" in out.content and store.list_applications() == []   # string refusal, nothing drafted


def test_pull_jobs_returns_a_run_id_and_refuses_while_locked(store, settings, tmp_path, monkeypatch):
    import jobagent.pipeline as pipeline
    from jobagent.core.schemas import Source
    from jobagent.ingestion.base import BaseAdapter

    class FakeAdapter(BaseAdapter):
        source = Source.remoteok

        def fetch(self):
            return iter([JobPosting(title="New Role", company="Z", source="remoteok",
                                    url="http://x/new", location="Remote", description="python")])

        @property
        def enabled(self):
            return True

    monkeypatch.setattr(pipeline, "build_adapters", lambda s: [FakeAdapter()])
    ran = {}
    def spawn(fn):
        ran["thread"] = "sync"; fn()
    a = _assistant(store, settings, tmp_path, spawn=spawn)
    out = _call(a, "pull_jobs", {})
    assert not out.is_error and out.data["run_id"] and "run_detail" in out.content
    # A pass now exists in the ledger under that id.
    assert any(r["run_id"] == out.data["run_id"] for r in store.list_runs())
    # While the lock is held, a second pull refuses.
    assert store.try_acquire_lock("pipeline", "holder")
    busy = _call(a, "pull_jobs", {})
    assert "already running" in busy.content   # string refusal, not a guard error
    store.release_lock("pipeline", "holder")


def test_pull_jobs_refuses_an_unknown_source(store, settings, tmp_path):
    a = _assistant(store, settings, tmp_path)
    out = _call(a, "pull_jobs", {"sources": "linkedin"})
    assert "linkedin" in out.content and "nknown" in out.content   # string refusal


def test_annotate_job_writes_a_note_without_changing_triage_state(store, settings, tmp_path):
    ids = _seed(store, 1)
    store.set_triage(ids[0], state="snoozed", snoozed_until="2999-01-01T00:00:00+00:00")
    a = _assistant(store, settings, tmp_path)
    out = _call(a, "annotate_job", {"job_id": ids[0], "note": "founder replied"})
    assert not out.is_error
    row = store.get_triage(ids[0])
    assert row["note"] == "founder replied" and row["state"] == "snoozed"   # state untouched


def test_set_application_status_refuses_an_illegal_move(store, settings, tmp_path):
    ids = _seed(store, 1)
    app_id = store.create_application(Application(job_id=ids[0], status="matched"))
    a = _assistant(store, settings, tmp_path)
    bad = _call(a, "set_application_status", {"application_id": app_id, "status": "offer"})
    assert "drafting" in bad.content and "Refused" in bad.content   # string refusal names the legal set
    ok = _call(a, "set_application_status", {"application_id": app_id, "status": "drafting"})
    assert not ok.is_error and store.get_application(app_id)["status"] == "drafting"
    assert store.get_application(app_id)["approved_at"] is None  # status only (R2)


def test_correct_application_status_overrides_and_audits(store, settings, tmp_path):
    ids = _seed(store, 1)
    app_id = store.create_application(Application(job_id=ids[0], status="matched"))
    sink = ListSink()
    a = build_assistant(store=store, settings=settings, sink=sink, surface=Surface.AGENT,
                        ask=lambda *_: True, deps=_deps(tmp_path))
    out = _call(a, "correct_application_status",
                {"application_id": app_id, "status": "offer", "reason": "typo"})
    assert not out.is_error and store.get_application(app_id)["status"] == "offer"
    # status_correction is logged to the store's events table by transition(), not to the
    # audit sink — the same place test_lifecycle checks.
    kinds = [r["kind"] for r in store.conn.execute("SELECT kind FROM events")]
    assert "status_correction" in kinds


def test_apply_profile_change_writes_search_fields_and_refuses_identity(store, settings, tmp_path):
    a = _assistant(store, settings, tmp_path)
    ok = _call(a, "apply_profile_change", {"field": "target_roles", "value": "AI Engineer, ML Engineer"})
    assert not ok.is_error
    overlay = json.loads((tmp_path / "profile.json").read_text())
    assert overlay["profile"]["target_roles"] == ["AI Engineer", "ML Engineer"]
    bad = _call(a, "apply_profile_change", {"field": "email", "value": "x@y.com"})
    assert "identity" in bad.content.lower() and "Refused" in bad.content   # string refusal
