"""Fit-checker: heuristic coverage + LLM report + assess_fit fallback. No network."""

from jobagent.fit import FitReport, assess_fit, heuristic_fit, llm_fit
from jobagent.preferences import Profile

PROFILE = Profile(
    headline="AI Engineer", seniority="mid-senior",
    target_roles=["AI Engineer", "Software Engineer"],
    core_skills=["Python", "FastAPI", "LangChain", "React", "Kubernetes"],
    keywords=["AI engineer", "agent", "LLM", "Python"],
)
CV = "Built LLM systems in Python with FastAPI and LangChain. Led React frontends."


def test_heuristic_strong_fit_high_score():
    job = {"title": "AI Engineer", "description": "Python, FastAPI, LangChain, building LLM agents."}
    r = heuristic_fit(job, PROFILE, CV)
    assert r.score >= 0.7
    assert "Python" in r.matched and "FastAPI" in r.matched
    assert r.source == "heuristic"


def test_heuristic_surfaces_gaps():
    # JD wants Kubernetes (in profile vocab, in JD) but the CV doesn't mention it → a gap.
    job = {"title": "AI Engineer", "description": "Python and Kubernetes for LLM serving."}
    r = heuristic_fit(job, PROFILE, CV)
    assert "Kubernetes" in r.missing
    assert "Python" in r.matched


def test_heuristic_weak_fit_low_score():
    r = heuristic_fit({"title": "Warehouse Operator", "description": "Lift boxes."}, PROFILE, CV)
    assert r.score < 0.2


def test_llm_fit_parses_report():
    class FakeLLM:
        def complete(self, system, user, json_mode=False):
            return ('{"confidence": 0.82, "matched": ["Python", "LLM"], '
                    '"missing": ["Kubernetes"], "experience": "mid-senior fits", "summary": "Strong."}')

    r = llm_fit({"title": "AI Engineer", "description": "..."}, PROFILE, CV, FakeLLM())
    assert r.source == "llm" and r.pct() == 82
    assert r.missing == ["Kubernetes"]


def test_assess_fit_falls_back_to_heuristic_on_llm_error():
    class BadLLM:
        def complete(self, *a, **k):
            raise RuntimeError("down")

    r = assess_fit({"title": "AI Engineer", "description": "Python FastAPI"}, PROFILE, CV, llm=BadLLM())
    assert r.source == "heuristic"      # gracefully degraded
    assert isinstance(r, FitReport)


def test_location_fit_remote_pref():
    p = Profile(must_haves=["remote"], exclude_locations=["US only"],
                target_roles=["AI Engineer"], core_skills=["Python"], keywords=["python"])
    remote = heuristic_fit({"title": "AI Engineer", "description": "Python", "is_remote": True, "location": "Remote"}, p, "Python")
    assert remote.location_fit is True
    onsite = heuristic_fit({"title": "AI Engineer", "description": "Python", "is_remote": False, "location": "Berlin"}, p, "Python")
    assert onsite.location_fit is False and "not remote" in onsite.location_note
    excluded = heuristic_fit({"title": "AI Engineer", "description": "Python", "is_remote": True, "location": "Remote - US only"}, p, "Python")
    assert excluded.location_fit is False and "excluded" in excluded.location_note


def test_format_short_contains_pct_and_sections():
    r = heuristic_fit({"title": "AI Engineer", "description": "Python FastAPI LangChain"}, PROFILE, CV)
    text = r.format_short()
    assert "Fit:" in text and "Matched:" in text and "Gaps:" in text
