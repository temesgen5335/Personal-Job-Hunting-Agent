"""Skill-gap aggregation + the learning-plan prompt. Offline (R17): pure functions plus
a FakeLLM. The aggregation is deterministic and does not touch a model."""

import json

from jobagent.core.schemas import JobPosting, Match, Source
from jobagent.store import Store
from jobagent.upskill import (
    learning_plan, learning_plan_prompt, skill_gaps, structural_notes, upskill_report,
)


def _rows(*specs):
    """specs: (title, company, score, gaps) → store-shaped match rows (gaps as JSON text)."""
    return [{"title": t, "company": c, "score": s, "gaps": json.dumps(g)}
            for (t, c, s, g) in specs]


def test_skill_gaps_rank_by_fit_weighted_frequency():
    rows = _rows(
        ("ML Eng", "A", 0.6, ["must-have not found: kubernetes", "must-have not found: aws"]),
        ("ML Eng", "B", 0.5, ["must-have not found: kubernetes"]),
        ("ML Eng", "C", 0.9, ["must-have not found: aws"]),
    )
    gaps = skill_gaps(rows, min_score=0.5)
    by = {g.skill: g for g in gaps}
    # kubernetes: 0.4 + 0.5 = 0.9 over 2 jobs; aws: 0.4 + 0.1 = 0.5 over 2 jobs.
    assert gaps[0].skill == "kubernetes"
    assert by["kubernetes"].count == 2
    assert round(by["kubernetes"].weight, 3) == 0.9
    assert round(by["aws"].weight, 3) == 0.5


def test_musthave_prefix_stripped_and_examples_carry_job_labels():
    rows = _rows(("Senior ML Engineer", "Acme", 0.6, ["must-have not found: Kubernetes"]))
    gaps = skill_gaps(rows, min_score=0.5)
    assert gaps[0].skill == "kubernetes"                 # prefix stripped, lowercased
    assert gaps[0].examples == ["Senior ML Engineer @ Acme"]


def test_weak_fit_jobs_excluded_by_min_score():
    rows = _rows(("Role", "Weak", 0.2, ["must-have not found: rust"]))
    assert skill_gaps(rows, min_score=0.5) == []          # below the floor, ignored


def test_structural_gaps_are_separated_from_skills():
    rows = _rows(
        ("Role", "A", 0.6, ["not clearly remote", "excluded: US only",
                            "seniority: wants 8+ years", "must-have not found: go"]),
    )
    gaps = skill_gaps(rows, min_score=0.5)
    notes = structural_notes(rows, min_score=0.5)
    assert [g.skill for g in gaps] == ["go"]              # only the real skill
    assert notes["location/remote"] == 1
    assert notes["hard-exclusion"] == 1
    assert notes["seniority"] == 1


def test_decode_gaps_tolerates_list_or_bad_json():
    rows = [
        {"title": "R", "company": "A", "score": 0.6, "gaps": ["must-have not found: sql"]},  # list
        {"title": "R", "company": "B", "score": 0.6, "gaps": "not json"},                    # junk
        {"title": "R", "company": "C", "score": 0.6, "gaps": None},                          # missing
    ]
    gaps = skill_gaps(rows, min_score=0.5)
    assert [g.skill for g in gaps] == ["sql"]             # junk/None contribute nothing


def test_learning_plan_prompt_lists_gaps_and_skills():
    rows = _rows(("Role", "A", 0.6, ["must-have not found: kubernetes"]))
    gaps = skill_gaps(rows, min_score=0.5)
    system, user = learning_plan_prompt(gaps, "Python, FastAPI")
    assert "learning plan" in system.lower()
    assert "kubernetes" in user
    assert "Python, FastAPI" in user


def test_learning_plan_is_empty_when_no_gaps():
    class Boom:
        def complete(self, *a, **k):
            raise AssertionError("must not call the model when there is nothing to plan")
    assert learning_plan([], "Python", Boom()) == ""


def test_learning_plan_calls_llm_when_gaps_present():
    class FakeLLM:
        def complete(self, system, user, json_mode=False):
            return "## Now\n- Kubernetes"
    rows = _rows(("Role", "A", 0.6, ["must-have not found: kubernetes"]))
    gaps = skill_gaps(rows, min_score=0.5)
    assert "Kubernetes" in learning_plan(gaps, "Python", FakeLLM())


def test_upskill_report_reads_the_store(tmp_path):
    store = Store(str(tmp_path / "u.db"))
    store.init_schema()
    jid = store.upsert_job(JobPosting(source=Source.telegram, title="ML Engineer",
                                      company="Acme", description="Kubernetes needed."))
    store.upsert_match(Match(job_id=jid, score=0.6,
                             gaps=["must-have not found: kubernetes"]))
    report = upskill_report(store, min_score=0.5)
    assert report["n_jobs"] == 1
    assert report["gaps"][0]["skill"] == "kubernetes"
    assert report["gaps"][0]["count"] == 1
    store.close()
