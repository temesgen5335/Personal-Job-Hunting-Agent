"""Heuristic scorer + engine tests (no LLM, no network)."""

import pytest

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


# --- Tier 2: preference-weighted scoring ------------------------------------------

WEIGHTED = Profile(
    target_roles=["AI Engineer"],
    seniority="mid-to-senior",
    core_skills=["Python", "LangChain", "RAG", "Docker", "AWS", "PostgreSQL", "CI/CD"],
    skill_weights={"LangChain": 2.0, "RAG": 2.0, "Python": 2.0,
                   "Docker": 0.5, "AWS": 0.5, "PostgreSQL": 0.5, "CI/CD": 0.5},
    must_haves=["remote"],
    keywords=["AI engineer"],
)


def test_differentiating_skills_outrank_generic_infra():
    """Four generic infra tools must not score like three differentiators — this is
    the main false-positive driver in a flat keyword count."""
    differentiating = heuristic_score(
        _job(title="AI Engineer", is_remote=1,
             description="Python, LangChain and RAG pipelines."), WEIGHTED)[0]
    generic = heuristic_score(
        _job(title="AI Engineer", is_remote=1,
             description="Docker, AWS, PostgreSQL and CI/CD maintenance."), WEIGHTED)[0]
    assert differentiating > generic


def test_heavy_skills_listed_first_in_rationale():
    _, rationale, _ = heuristic_score(
        _job(title="AI Engineer", is_remote=1,
             description="Docker, AWS, Python, LangChain."), WEIGHTED)
    skills = rationale.split("skills: ")[1].split(";")[0]
    assert skills.index("LangChain") < skills.index("Docker")


def test_role_in_title_beats_keyword_in_title_beats_body_mention():
    p = Profile(target_roles=["AI Engineer"], keywords=["python"],
                core_skills=["Python"], seniority="mid")
    in_title = heuristic_score(_job(title="AI Engineer", description="work"), p)[0]
    kw_title = heuristic_score(_job(title="Python Developer", description="work"), p)[0]
    in_body = heuristic_score(_job(title="Analyst", description="team of AI Engineer folks"), p)[0]
    assert in_title > kw_title > in_body


def test_junior_posting_penalized_for_senior_profile():
    senior = heuristic_score(
        _job(title="AI Engineer", is_remote=1, description="Python LangChain RAG"), WEIGHTED)
    junior = heuristic_score(
        _job(title="Junior AI Engineer", is_remote=1, description="Python LangChain RAG"), WEIGHTED)
    assert junior[0] < senior[0]
    assert any("level mismatch" in g for g in junior[2])


def test_management_track_penalized_for_ic_profile():
    score, _, gaps = heuristic_score(
        _job(title="Head of Engineering", is_remote=1, description="Python LangChain RAG"), WEIGHTED)
    assert any("management-track" in g for g in gaps)
    assert score < heuristic_score(
        _job(title="AI Engineer", is_remote=1, description="Python LangChain RAG"), WEIGHTED)[0]


def test_entry_level_profile_not_penalized_for_junior_roles():
    """The penalty is relative to the profile, not absolute."""
    grad = Profile(target_roles=["AI Engineer"], seniority="junior", core_skills=["Python"])
    _, _, gaps = heuristic_score(_job(title="Junior AI Engineer", description="Python"), grad)
    assert not any("level mismatch" in g for g in gaps)


def test_manager_profile_not_penalized_for_management_roles():
    mgr = Profile(target_roles=["Engineering Manager"], seniority="senior manager",
                  core_skills=["Python"])
    _, _, gaps = heuristic_score(
        _job(title="Head of Engineering", description="Python"), mgr)
    assert not any("management-track" in g for g in gaps)


def test_non_remote_must_haves_are_checked():
    """Previously only 'remote' was honored; other must-haves were dead config."""
    p = Profile(target_roles=["AI Engineer"], core_skills=["Python"],
                must_haves=["remote", "visa sponsorship"], seniority="mid")
    _, _, gaps = heuristic_score(
        _job(title="AI Engineer", is_remote=1, description="Python role"), p)
    assert any("visa sponsorship" in g for g in gaps)
    # Present in the text → no gap.
    _, _, gaps2 = heuristic_score(
        _job(title="AI Engineer", is_remote=1,
             description="Python role with visa sponsorship available"), p)
    assert not any("visa sponsorship" in g for g in gaps2)


def test_negative_skill_weight_is_clamped_not_inverted():
    """A negative weight passes validation but must not turn a match into a penalty."""
    p = Profile(target_roles=["AI Engineer"], core_skills=["Python"],
                skill_weights={"Python": -5.0}, seniority="mid")
    neutral = Profile(target_roles=["AI Engineer"], core_skills=["Python"], seniority="mid")
    job = _job(title="AI Engineer", description="Python")
    # Clamped to 0 → contributes nothing, but never subtracts.
    assert 0 < heuristic_score(job, p)[0] <= heuristic_score(job, neutral)[0]


def test_non_numeric_skill_weight_is_rejected_at_load():
    """Config typos fail loudly at startup rather than silently skewing every score."""
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        Profile(core_skills=["Python"], skill_weights={"Python": "heavy"})  # type: ignore[arg-type]


def test_skill_weights_are_case_insensitive():
    p = Profile(target_roles=["AI Engineer"], core_skills=["LangChain"],
                skill_weights={"langchain": 2.0}, seniority="mid")
    flat = Profile(target_roles=["AI Engineer"], core_skills=["LangChain"], seniority="mid")
    job = _job(title="AI Engineer", description="LangChain")
    assert heuristic_score(job, p)[0] > heuristic_score(job, flat)[0]


def test_score_stays_in_range_under_stacked_penalties():
    p = Profile(target_roles=["AI Engineer"], core_skills=["Python"],
                must_haves=["remote", "async"], exclude_keywords=["unpaid"], seniority="mid")
    score, _, gaps = heuristic_score(
        _job(title="Junior AI Engineer intern", location="Onsite NYC",
             description="unpaid Python position"), p)
    assert 0.0 <= score <= 1.0
    assert len(gaps) >= 3


def test_seniority_terms_are_word_boundary_matched():
    """Substring matching flagged 'Semiconductors' and 'Connectors' as CTO roles —
    the same class of bug that once made 'Go' match 'going'."""
    for title in ("Technical Deployment Lead, Semiconductors",
                  "Researcher, Connectors - Agent Post-Training",
                  "Interned Systems Analyst"):
        _, _, gaps = heuristic_score(_job(title=title, is_remote=1, description="Python"), WEIGHTED)
        assert not any("management-track" in g or "level mismatch" in g for g in gaps), title
    # A real CTO posting is still caught.
    _, _, gaps = heuristic_score(_job(title="CTO / Head of AI", is_remote=1, description="Python"), WEIGHTED)
    assert any("management-track" in g for g in gaps)


# --- geographic eligibility (config-driven; matching/heuristic.py) -----------

# remote_scope="global": only genuinely global-remote postings are eligible.
_GLOBAL = Profile(
    target_roles=["AI Engineer"], core_skills=["Python", "LangChain", "RAG"],
    must_haves=["remote"], keywords=["AI engineer", "LLM"], remote_scope="global",
)
# remote_scope defaults to "any": geo scoring is OFF (the reusable default).
_OFF = Profile(
    target_roles=["AI Engineer"], core_skills=["Python", "LangChain", "RAG"],
    must_haves=["remote"], keywords=["AI engineer", "LLM"],
)
_STRONG_DESC = "Build agentic LLM systems with LangChain and RAG in Python."


@pytest.mark.parametrize("loc", [
    "Remote - US", "Remote (US)", "San Francisco", "New York, NY (HQ)",
    "Remote - California", "Remote - CA", "Canada - Remote (ON, AB)", "China - Remote",
    "Remote, Bangalore", "Ukraine Anywhere", "Remote - EMEA", "Remote - Europe",
    "Americas, Europe, Asia, Africa",
])
def test_global_scope_locks_any_place_pinned_location(loc):
    """Under global scope, ANY location that names a place — country, region, state or
    city, gazetteer-free — is region-locked and capped."""
    score, _, gaps = heuristic_score(
        _job(title="AI Engineer", is_remote=1, location=loc, description=_STRONG_DESC), _GLOBAL)
    assert any("region-locked" in g for g in gaps), (loc, gaps)
    assert score <= 0.16


@pytest.mark.parametrize("loc", [
    "Remote", "Remote - Worldwide", "Anywhere", "Fully Remote", "Remote (Global)",
    "Work from anywhere", "",
])
def test_global_scope_keeps_genuinely_global_remote(loc):
    score, _, gaps = heuristic_score(
        _job(title="AI Engineer", is_remote=1, location=loc, description=_STRONG_DESC), _GLOBAL)
    assert not any("region-locked" in g for g in gaps), (loc, gaps)
    assert score > 0.5


def test_global_scope_locks_a_region_requirement_in_the_body():
    """The location says 'Remote', but the body pins it to a region."""
    job = _job(title="AI Engineer", is_remote=1, location="Remote",
               description=_STRONG_DESC + " You must be authorized to work in the United States.")
    score, _, gaps = heuristic_score(job, _GLOBAL)
    assert any("region-locked" in g for g in gaps)
    assert score <= 0.16


def test_geo_scoring_is_off_by_default():
    """A profile that has not opted in (remote_scope='any') is unaffected — no lock."""
    for loc in ("San Francisco", "Remote - US", "Canada - Remote"):
        _, _, gaps = heuristic_score(
            _job(title="AI Engineer", is_remote=1, location=loc, description=_STRONG_DESC), _OFF)
        assert not any("region-locked" in g for g in gaps), loc


def test_geo_eligible_include_list_overrides_a_lock():
    """A user can allow-list a region; then it is not locked even under global scope."""
    prof = _GLOBAL.model_copy(update={"geo_eligible": ["emea"]})
    score, _, gaps = heuristic_score(
        _job(title="AI Engineer", is_remote=1, location="Remote - EMEA", description=_STRONG_DESC), prof)
    assert not any("region-locked" in g for g in gaps)
    assert score > 0.5


def test_geo_blocked_list_demotes_even_when_scope_is_any():
    """The exclude list is honoured without turning on global-only mode."""
    prof = _OFF.model_copy(update={"geo_blocked": ["united states", "remote - uk"]})
    score, _, gaps = heuristic_score(
        _job(title="AI Engineer", is_remote=1, location="Remote - United States",
             description=_STRONG_DESC), prof)
    assert any("excluded location" in g for g in gaps)
    assert score <= 0.16


def test_custom_global_terms_replace_the_default_vocabulary():
    """geo_global_terms lets the user redefine what 'globally open' means."""
    prof = _GLOBAL.model_copy(update={"geo_global_terms": ["worldwide", "latam"]})
    _, _, allow = heuristic_score(                       # LATAM now counts as global...
        _job(title="AI Engineer", is_remote=1, location="Remote - LATAM", description=_STRONG_DESC), prof)
    assert not any("region-locked" in g for g in allow)
    _, _, lock = heuristic_score(                        # ...a place not in the list stays locked
        _job(title="AI Engineer", is_remote=1, location="Remote - US", description=_STRONG_DESC), prof)
    assert any("region-locked" in g for g in lock)


def test_region_code_not_matched_inside_a_place_name():
    """The classic substring bug: a body mention of 'Belarus' must not read as a US lock,
    and a worldwide location keeps the posting eligible."""
    job = _job(title="AI Engineer", is_remote=1, location="Remote - Worldwide",
               description=_STRONG_DESC + " Our team spans Belarus and Austria.")
    _, _, gaps = heuristic_score(job, _GLOBAL)
    assert not any("region-locked" in g for g in gaps)
