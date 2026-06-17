"""Heuristic scorer + engine tests (no LLM, no network)."""

from jobagent.core.schemas import JobPosting, Source
from jobagent.matching import heuristic_score, run_matching
from jobagent.preferences import Profile
from jobagent.store import Store

PROFILE = Profile(
    target_roles=["AI Engineer", "Frontend Engineer"],
    core_skills=["Python", "Next.js", "FastAPI", "LangChain", "React"],
    domains=["agentic AI", "developer tools"],
    must_haves=["remote"],
    exclude_keywords=["on-site only", "clearance required"],
    keywords=["AI engineer", "agent", "LLM", "frontend", "Next.js"],
)


def _job(**kw) -> dict:
    base = {"title": "", "description": "", "is_remote": 0, "tags": "[]", "company": "X",
            "location": "", "source": "remoteok", "apply_url": "", "url": ""}
    base.update(kw)
    return base


def test_strong_match_outranks_irrelevant():
    strong, rationale, gaps = heuristic_score(
        _job(title="Senior AI Engineer", is_remote=1,
             description="Build agentic LLM systems in Python with FastAPI and Next.js."),
        PROFILE,
    )
    weak, _, _ = heuristic_score(
        _job(title="Warehouse Forklift Operator", description="Lift boxes on-site."),
        PROFILE,
    )
    assert strong >= 0.6           # clearly relevant
    assert weak < 0.2              # clearly irrelevant
    assert strong > weak
    assert "skills" in rationale
    assert gaps == []


def test_word_boundary_no_substring_false_hits():
    # "Go" must not match "ongoing"/"category"; "RAG" must not match "fragment".
    score, rationale, _ = heuristic_score(
        _job(title="Category Manager",
             description="Ongoing fragment cataloguing. No engineering."),
        PROFILE,
    )
    assert "Go" not in rationale
    assert "RAG" not in rationale
    assert score < 0.2


def test_non_remote_penalized_and_gap_noted():
    score, _, gaps = heuristic_score(
        _job(title="AI Engineer", is_remote=0, location="NYC office",
             description="ML in Python. on-site only."),
        PROFILE,
    )
    assert any("remote" in g for g in gaps)
    assert any("excluded" in g for g in gaps)
    assert score <= 0.15  # excluded keyword caps the score


def test_engine_scores_and_persists(tmp_path):
    store = Store(str(tmp_path / "m.db"))
    store.init_schema()
    store.upsert_job(JobPosting(source=Source.remoteok, title="AI Engineer (Agentic)",
                                company="Acme", is_remote=True,
                                description="LangChain, Python, FastAPI, agents."))
    store.upsert_job(JobPosting(source=Source.remoteok, title="Plumber", company="Pipes",
                                description="Fix pipes."))
    report = run_matching(store, PROFILE)  # no key → heuristic only
    assert report.scored == 2
    assert report.used_llm is False

    top = store.get_top_matches(limit=5, min_score=0.0)
    assert top[0]["title"].startswith("AI Engineer")  # best match ranks first
    store.close()
