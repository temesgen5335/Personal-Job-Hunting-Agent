"""Filter query (store.get_matches) + menu filter-state logic. No telegram, no network."""

from datetime import datetime, timedelta, timezone

from jobagent.bot.service import (
    MatchFilter,
    apply_menu_action,
    filter_summary,
    ranked_matches,
    set_keywords,
)
from jobagent.core.schemas import JobPosting, Match, Source
from jobagent.store import Store


def _store(tmp_path):
    s = Store(str(tmp_path / "f.db"))
    s.init_schema()
    return s


def _add(store, title, *, score, remote=False, location="", posted_days_ago=0, desc="", tags=None):
    posted = datetime.now(timezone.utc) - timedelta(days=posted_days_ago)
    jid = store.upsert_job(JobPosting(
        source=Source.remoteok, title=title, company=title, is_remote=remote,
        location=location, description=desc, tags=tags or [], posted_at=posted,
    ))
    store.upsert_match(Match(job_id=jid, score=score))
    return jid


def test_date_filter(tmp_path):
    s = _store(tmp_path)
    _add(s, "Fresh", score=0.9, posted_days_ago=0)
    _add(s, "Old", score=0.95, posted_days_ago=40)
    recent = s.get_matches(limit=10, max_age_days=7)
    titles = {r["title"] for r in recent}
    assert "Fresh" in titles and "Old" not in titles
    assert len(s.get_matches(limit=10)) == 2  # no filter → both
    s.close()


def test_location_filter(tmp_path):
    s = _store(tmp_path)
    _add(s, "RemoteRole", score=0.9, remote=True, location="Remote - US")
    _add(s, "HybridRole", score=0.9, remote=False, location="Hybrid - NYC")
    _add(s, "OnsiteRole", score=0.9, remote=False, location="NYC office")
    assert {r["title"] for r in s.get_matches(location="remote")} == {"RemoteRole"}
    assert {r["title"] for r in s.get_matches(location="hybrid")} == {"HybridRole"}
    assert len(s.get_matches(location="any")) == 3
    s.close()


def test_keyword_filter_matches_any(tmp_path):
    s = _store(tmp_path)
    _add(s, "Frontend Engineer", score=0.9, desc="React and Next.js")
    _add(s, "Backend Engineer", score=0.9, desc="Go services")
    _add(s, "Data Scientist", score=0.9, tags=["python", "ml"])
    got = {r["title"] for r in s.get_matches(keywords=["frontend", "python"])}
    assert got == {"Frontend Engineer", "Data Scientist"}   # OR-match across title/desc/tags
    s.close()


def test_ranked_matches_applies_filter(tmp_path):
    s = _store(tmp_path)
    _add(s, "RemoteAI", score=0.9, remote=True, location="Remote")
    _add(s, "OnsiteAI", score=0.95, remote=False, location="Berlin")
    ranked = ranked_matches(s, 10, MatchFilter(location="remote"))
    assert [r["title"] for r in ranked] == ["RemoteAI"]
    s.close()


def test_exclude_and_include_locations(tmp_path):
    s = _store(tmp_path)
    _add(s, "RemoteUS", score=0.9, location="Remote - US only")
    _add(s, "RemoteWW", score=0.9, location="Remote - Worldwide")
    _add(s, "IndiaRole", score=0.9, location="Bangalore, India")
    # exclude: drop US-only and India
    got = {r["title"] for r in s.get_matches(exclude_locations=["US only", "India"])}
    assert got == {"RemoteWW"}
    # include (keep-only): only worldwide
    got2 = {r["title"] for r in s.get_matches(include_locations=["worldwide"])}
    assert got2 == {"RemoteWW"}
    s.close()


def test_offset_pagination(tmp_path):
    s = _store(tmp_path)
    for i in range(5):
        _add(s, f"Job{i}", score=0.9 - i * 0.1, location="Remote")
    page1 = [r["title"] for r in s.get_matches(limit=2, offset=0)]
    page2 = [r["title"] for r in s.get_matches(limit=2, offset=2)]
    assert page1 == ["Job0", "Job1"] and page2 == ["Job2", "Job3"]
    s.close()


def test_filter_from_profile_remote_default():
    from jobagent.preferences import Profile
    flt = MatchFilter.from_profile(Profile(must_haves=["remote"], exclude_locations=["US only"],
                                           preferred_locations=["worldwide"]))
    assert flt.location == "remote"
    assert flt.exclude_locations == ["US only"]
    assert flt.include_locations == ["worldwide"]
    # no remote must-have → any
    assert MatchFilter.from_profile(Profile(must_haves=[])).location == "any"


def test_menu_action_updates_filter():
    flt = MatchFilter()
    apply_menu_action(flt, "date", "7")
    assert flt.max_age_days == 7
    apply_menu_action(flt, "date", "0")
    assert flt.max_age_days is None          # 0 → any time
    apply_menu_action(flt, "loc", "remote")
    assert flt.location == "remote"
    apply_menu_action(flt, "loc", "bogus")
    assert flt.location == "any"             # invalid → any


def test_set_keywords_and_summary():
    flt = set_keywords(MatchFilter(), "frontend, react  ai")
    assert flt.keywords == ["frontend", "react", "ai"]
    apply_menu_action(flt, "kwclear", "")
    assert flt.keywords == []
    summary = filter_summary(MatchFilter(max_age_days=1, location="remote"))
    assert "today" in summary and "remote" in summary
