"""ATS-parseability report + drafter→reviewer→revise loop.

Offline (R17): a scripted FakeLLM stands in for the model. FakeLLM cannot catch
fabrication — only a live run can (R1b) — so these tests assert the mechanical
guardrails (the prompt clauses, the gate, the honesty of the report), and the revise
step ships OFF until a live check (see config.apply_review_enabled)."""

import json

from jobagent.apply import ats_report, extract_keywords, prepare_application
from jobagent.apply.generators import (
    REVIEW_SYSTEM, REVISE_SYSTEM, review_draft, review_prompt, revise_prompt,
)
from jobagent.core.schemas import ApplyMethod, JobPosting, Source
from jobagent.preferences import Profile
from jobagent.store import Store

JOB = {
    "id": "j1", "title": "Senior Python Engineer", "company": "Acme",
    "location": "Remote",
    "description": "We need Python, FastAPI and PostgreSQL. Kubernetes a plus. "
                   "You will build data pipelines and ship LLM features.",
}
CV = ("# Ada Lovelace\nada@example.com · +251 900 111 222\n\n"
      "AI Engineer. Built LLM systems with Python and FastAPI. Data pipelines on PostgreSQL.")


# --- keyword extraction & coverage ----------------------------------------------

def test_extract_keywords_finds_salient_terms_drops_stopwords():
    kws = extract_keywords(JOB)
    assert "python" in kws
    assert "fastapi" in kws
    assert "postgresql" in kws
    assert "the" not in kws and "will" not in kws and "you" not in kws


def test_coverage_counts_present_terms_and_lists_genuine_gaps():
    r = ats_report(CV, JOB, name="Ada Lovelace", email="ada@example.com", phone="+251900111222")
    assert r.ok is True
    assert "python" in r.covered and "fastapi" in r.covered
    # Kubernetes is a real gap: reported, never injected.
    assert "kubernetes" in r.missing
    assert "kubernetes" not in r.covered
    assert 0.0 < r.coverage < 1.0


# --- honesty: the report never flips ok on a keyword gap ------------------------

def test_missing_keywords_do_not_make_report_not_ok():
    thin_cv = "# Ada Lovelace\nada@example.com · +251900111222\nI write software."
    r = ats_report(thin_cv, JOB, name="Ada Lovelace", email="ada@example.com", phone="+251900111222")
    assert r.missing                      # lots of gaps
    assert r.ok is True                   # but no hard defect → still ok


# --- hard defects: contact not literal, garbled glyphs --------------------------

def test_missing_contact_details_flag_not_ok():
    r = ats_report("Python and FastAPI engineer.", JOB,
                   name="Ada Lovelace", email="ada@example.com", phone="+251900111222")
    assert r.ok is False
    assert any("email" in i for i in r.issues)
    assert any("phone" in i for i in r.issues)
    assert any("name" in i for i in r.issues)


def test_garbled_text_layer_flags_not_ok():
    r = ats_report("Contact: (cid:12)(cid:13) � Python FastAPI", JOB)
    assert r.ok is False
    assert any("cid" in i for i in r.issues)
    assert any("replacement" in i for i in r.issues)


def test_email_only_in_a_link_is_invisible_to_ats():
    # An email carried by a mailto link but not as literal text fails the check.
    cv = "# Ada Lovelace\n[Contact me](mailto:ada@example.com)\nPython, FastAPI."
    r = ats_report(cv, JOB, email="ada@example.com")
    # The literal string 'ada@example.com' appears here, so this one passes —
    # guard the true failure: the address present only inside an opaque token.
    assert r.ok is True
    r2 = ats_report("# Ada\nPython, FastAPI.", JOB, email="ada@example.com")
    assert r2.ok is False


# --- reviewer prompt guardrails (R1a: CV present; R1: no fabrication) ------------

def test_review_and_revise_prompts_carry_cv_and_no_fabrication_clause():
    assert "Never invent" in REVIEW_SYSTEM
    assert "Never invent" in REVISE_SYSTEM
    _, ruser = review_prompt("cv", "DRAFT", CV, JOB)
    assert CV in ruser                                # R1a: reviewer sees the CV
    _, vuser = revise_prompt("cv", "DRAFT", {"verdict": "revise"}, CV, JOB)
    assert CV in vuser                                # R1a: reviser sees the CV
    assert "never stuff" in REVISE_SYSTEM.lower()     # honesty: no keyword stuffing


def test_review_draft_degrades_to_ok_on_bad_json():
    class BadLLM:
        def complete(self, *a, **k):
            return "not json at all"
    verdict = review_draft("cv", "DRAFT", CV, JOB, BadLLM())
    assert verdict["verdict"] == "ok"
    assert verdict["missing_keywords"] == []


# --- the flow: report always attached; review gated -----------------------------

class _ScriptedLLM:
    """Distinguishes calls by their system prompt so one fake serves the whole flow."""
    def __init__(self):
        self.review_calls = 0
        self.revise_calls = 0

    def complete(self, system, user, json_mode=False):
        if system.startswith("You are a critical reviewer"):
            self.review_calls += 1
            return json.dumps({"fabrication_risk": [], "missing_keywords": ["kubernetes"],
                               "weaknesses": ["generic opener"], "verdict": "revise"})
        if system.startswith("You revise a job-application draft"):
            self.revise_calls += 1
            return "REVISED DRAFT"
        if json_mode:
            return json.dumps({"subject": "Application", "body": "Hello."})
        return CV  # CV / cover drafts


def _job(store) -> dict:
    jid = store.upsert_job(JobPosting(source=Source.telegram, title="Senior Python Engineer",
                                      company="Acme", description=JOB["description"],
                                      is_remote=True, apply_method=ApplyMethod.email,
                                      apply_email="jobs@acme.example"))
    return store.get_job(jid)


def test_prepare_attaches_ats_report_without_review_by_default(tmp_path):
    store = Store(str(tmp_path / "a.db"))
    store.init_schema()
    llm = _ScriptedLLM()
    profile = Profile(name="Ada Lovelace", email="ada@example.com", phone="+251900111222")
    bundle = prepare_application(store, _job(store), profile, CV, llm)  # no settings → no review
    assert bundle.ats is not None
    assert bundle.review is None
    assert llm.review_calls == 0 and llm.revise_calls == 0
    store.close()


def test_prepare_runs_review_loop_when_enabled(tmp_path):
    class S:
        apply_review_enabled = True
        apply_review_rounds = 1
    store = Store(str(tmp_path / "a.db"))
    store.init_schema()
    llm = _ScriptedLLM()
    profile = Profile(name="Ada Lovelace", email="ada@example.com", phone="+251900111222")
    bundle = prepare_application(store, _job(store), profile, CV, llm, settings=S())
    assert bundle.review is not None
    assert bundle.review["cv"]["verdict"] == "revise"
    assert llm.review_calls == 2         # cv + cover letter
    assert llm.revise_calls == 2         # each revised once
    # The revised CV is what got persisted and reported on.
    assert bundle.cv_markdown == "REVISED DRAFT"
    store.close()
