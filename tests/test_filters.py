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
