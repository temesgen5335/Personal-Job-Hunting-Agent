"""Store retention (`prune_jobs`).

Retention is distinct from the ingest gate: the gate decides what to store, this
decides how long stored postings live. The rule that matters most is that anything
you acted on survives regardless of age — that is your own history, not scrape data.
"""

from datetime import datetime, timedelta, timezone

from jobagent.core.schemas import Application, ApplyMethod, CVVariant, JobPosting, Match, Source
from jobagent.store import Store


def _store(tmp_path):
    s = Store(str(tmp_path / "p.db"))
    s.init_schema()
    return s


def _job(store, title, *, days_old=0, score=0.8):
    jid = store.upsert_job(JobPosting(source=Source.remoteok, title=title, company=title,
                                      location="Remote", is_remote=True))
    store.upsert_match(Match(job_id=jid, score=score))
    if days_old:
        ts = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
        store.conn.execute("UPDATE jobs SET last_seen_at=? WHERE id=?", (ts, jid))
        store.conn.commit()
    return jid


def test_prunes_only_what_is_stale(tmp_path):
    s = _store(tmp_path)
    _job(s, "Old", days_old=90)
    fresh = _job(s, "Fresh", days_old=1)
    out = s.prune_jobs(older_than_days=30)
    assert out["jobs"] == 1
    assert [j["id"] for j in s.get_jobs()] == [fresh]
    s.close()


def test_dependent_rows_go_with_the_job(tmp_path):
    """FK enforcement is ON, so matches/triage must be removed first or the delete
    is rejected outright."""
    s = _store(tmp_path)
    jid = _job(s, "Old", days_old=90)
    s.set_triage(jid, state="dismissed")
    out = s.prune_jobs(older_than_days=30)
    assert out["jobs"] == 1 and out["matches"] == 1 and out["triage"] == 1
    assert s.conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 0
    assert s.conn.execute("SELECT COUNT(*) FROM triage").fetchone()[0] == 0
    s.close()


def test_a_job_you_applied_to_is_never_pruned(tmp_path):
    """Your application history is not scrape data. Pruning it would orphan the
    application row and destroy the record that you applied at all."""
    s = _store(tmp_path)
    jid = _job(s, "Applied", days_old=400)
    s.create_application(Application(job_id=jid, apply_method=ApplyMethod.email))
    out = s.prune_jobs(older_than_days=30)
    assert out["jobs"] == 0 and out["kept_acted_on"] == 1
    assert s.count_jobs() == 1
    assert len(s.list_applications()) == 1        # history intact
    s.close()


def test_a_job_with_a_tailored_cv_is_never_pruned(tmp_path):
    s = _store(tmp_path)
    jid = _job(s, "Drafted", days_old=400)
    s.insert_cv_variant(CVVariant(job_id=jid, base_cv_id="master", content_markdown="..."))
    assert s.prune_jobs(older_than_days=30)["jobs"] == 0
    assert s.count_jobs() == 1
    s.close()


def test_prune_is_a_noop_when_nothing_is_stale(tmp_path):
    s = _store(tmp_path)
    _job(s, "Fresh", days_old=1)
    assert s.prune_jobs(older_than_days=30) == {
        "jobs": 0, "matches": 0, "triage": 0, "kept_acted_on": 0}
    assert s.count_jobs() == 1
    s.close()


def test_age_is_last_seen_not_first_seen(tmp_path):
    """A posting still appearing in today's feed is live, however old the listing is."""
    s = _store(tmp_path)
    jid = _job(s, "LongLived", days_old=0)
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    s.conn.execute("UPDATE jobs SET first_seen_at=? WHERE id=?", (old, jid))
    s.conn.commit()
    assert s.prune_jobs(older_than_days=30)["jobs"] == 0     # still seen today → keep
    s.close()


def test_queue_and_stats_survive_a_prune(tmp_path):
    s = _store(tmp_path)
    _job(s, "OldWeak", days_old=90, score=0.2)
    _job(s, "FreshStrong", days_old=1, score=0.9)
    s.prune_jobs(older_than_days=30)
    st = s.stats()
    assert st["total_jobs"] == 1 and st["queue"] == 1 and st["matches"] == 1
    s.close()


def test_vacuum_is_opt_in_and_safe(tmp_path):
    s = _store(tmp_path)
    _job(s, "Old", days_old=90)
    out = s.prune_jobs(older_than_days=30, vacuum=True)
    assert out["jobs"] == 1
    assert s.count_jobs() == 0          # db still usable after VACUUM
    s.close()
