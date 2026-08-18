"""Filtered job cleanup — the destructive path, so every test here is about what
must NOT be deleted as much as what must.

The governing property: preview and apply are the same query, differing only in
whether the DELETE runs. If they can diverge, a user approves one row set and loses
a different one.
"""

import pytest

from jobagent.core.schemas import (
    Application,
    ApplyMethod,
    CVVariant,
    JobPosting,
    Match,
    Source,
)
from jobagent.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "purge.db"))
    s.init_schema()
    yield s
    s.close()


def _job(store, title, *, score=0.5, company="Acme", source=Source.remoteok,
         scored=True, remote=True, location="Remote", description=""):
    jid = store.upsert_job(JobPosting(source=source, title=title, company=company,
                                      is_remote=remote, location=location,
                                      description=description))
    if scored:
        store.upsert_match(Match(job_id=jid, score=score, rationale="r"))
    return jid


# --- the parity property -----------------------------------------------------

def test_preview_and_apply_select_the_identical_rows(store):
    """The one property the whole feature rests on. A preview the user approves and
    the delete that follows must be the same query — otherwise they approve one set
    and lose another, which is the 2026-08-17 queue bug with teeth."""
    for i in range(6):
        _job(store, f"Weak {i}", score=0.2)
    for i in range(3):
        _job(store, f"Strong {i}", score=0.9)

    preview = store.purge_jobs(max_score=0.7)
    assert preview["dry_run"] is True
    assert preview["jobs"] == 6
    assert store.count_jobs() == 9, "a preview must not delete anything"

    applied = store.purge_jobs(max_score=0.7, dry_run=False)
    assert applied["jobs"] == preview["jobs"] == 6
    assert applied["matches"] == preview["matches"] == 6
    assert store.count_jobs() == 3


def test_dry_run_defaults_to_true(store):
    """A caller that forgets the flag previews. The destructive reading has to be
    typed out, never fallen into."""
    _job(store, "Weak", score=0.1)
    result = store.purge_jobs(max_score=0.7)          # no dry_run passed
    assert result["dry_run"] is True
    assert result["jobs"] == 1                        # it WOULD go
    assert store.count_jobs() == 1                    # but it did not


# --- what must survive -------------------------------------------------------

def test_a_job_with_an_application_survives_every_filter(store):
    """Unconditional, not a checkbox: deleting it orphans the application record."""
    jid = _job(store, "Applied To", score=0.1, company="Zzz", source=Source.lever)
    store.create_application(Application(job_id=jid, apply_method=ApplyMethod.email))

    for kwargs in ({"max_score": 0.7}, {"last_seen_days": 0}, {"min_score": 0.0},
                   {"sources": ["lever"]}, {"companies": ["Zzz"]},
                   {"triage_states": ["untriaged"]}, {"location": "remote"}):
        result = store.purge_jobs(dry_run=False, **kwargs)
        assert result["jobs"] == 0, f"{kwargs} deleted an applied-to job"
    assert store.get_job(jid) is not None


def test_a_tailored_cv_also_protects_a_job(store):
    jid = _job(store, "Has CV", score=0.1)
    store.insert_cv_variant(CVVariant(job_id=jid, content_markdown="tailored", base_cv_id="base"))
    result = store.purge_jobs(max_score=0.7, dry_run=False)
    assert result["jobs"] == 0
    assert result["kept_acted_on"] == 1, "spared rows must be reported, not silently skipped"
    assert store.get_job(jid) is not None


def test_a_triage_note_protects_a_job(store):
    """Settled in the plan: a note is the operator's own writing, so a bulk sweep
    does not get to discard it. Dismissing alone is NOT protection — that is a
    decision to be rid of it."""
    noted = _job(store, "Noted", score=0.1)
    dismissed = _job(store, "Dismissed", score=0.1)
    store.set_triage(noted, note="revisit in autumn")
    store.set_triage(dismissed, state="dismissed")

    result = store.purge_jobs(max_score=0.7, dry_run=False)
    assert store.get_job(noted) is not None, "a note must protect the row"
    assert store.get_job(dismissed) is None, "a dismissal must not"
    assert result["jobs"] == 1 and result["kept_acted_on"] == 1


# --- mechanics ---------------------------------------------------------------

def test_fk_order_holds_for_a_job_carrying_matches_and_triage(store):
    """PRAGMA foreign_keys = ON rejects deleting a job whose dependents are still
    there. Regression guard: this raised before dependents were removed first."""
    jid = _job(store, "Full House", score=0.2)
    store.set_triage(jid, state="dismissed")
    result = store.purge_jobs(max_score=0.7, dry_run=False)   # must not raise
    assert result["jobs"] == 1 and result["matches"] == 1 and result["triage"] == 1
    assert store.get_job(jid) is None


def test_unscored_jobs_are_reachable_only_when_asked_for(store):
    """A never-matched job has a NULL score, and `NULL < 0.7` is NULL — so without
    the explicit flag a score filter silently skips every unscored row."""
    _job(store, "Never Scored", scored=False)
    _job(store, "Weak", score=0.2)

    assert store.purge_jobs(max_score=0.7)["jobs"] == 1
    assert store.purge_jobs(max_score=0.7, include_unscored=True)["jobs"] == 2

    store.purge_jobs(max_score=0.7, include_unscored=True, dry_run=False)
    assert store.count_jobs() == 0


def test_an_unfiltered_purge_selects_nothing(store):
    """No predicate at all would mean the whole store. That is a caller bug far more
    often than an intent, so it is refused rather than guessed at."""
    _job(store, "Keep me", score=0.9)
    result = store.purge_jobs(dry_run=False)
    assert result["unfiltered"] is True and result["jobs"] == 0
    assert store.count_jobs() == 1


def test_filters_compose_rather_than_widen(store):
    """Two filters must intersect. A purge that ORed them would delete far more than
    the preview implied."""
    _job(store, "Weak remoteok", score=0.2, source=Source.remoteok)
    _job(store, "Weak lever", score=0.2, source=Source.lever)
    _job(store, "Strong remoteok", score=0.9, source=Source.remoteok)

    result = store.purge_jobs(max_score=0.7, sources=["remoteok"], dry_run=False)
    assert result["jobs"] == 1
    assert {j["title"] for j in store.get_jobs()} == {"Weak lever", "Strong remoteok"}


def test_the_sample_is_capped_but_the_count_is_not(store):
    """R32: query wider than you show, so the cap can never read as the total."""
    for i in range(20):
        _job(store, f"Weak {i}", score=0.1)
    result = store.purge_jobs(max_score=0.7, sample_size=5)
    assert result["jobs"] == 20, "the count is the real number"
    assert len(result["sample"]) == 5, "the sample is only a sample"


def test_a_purge_drops_the_stale_knowledge_index(store):
    """The FTS index is derived data that does NOT notice deletions — it is refreshed
    only by a full rebuild. Left behind, the assistant keeps citing postings that no
    longer exist: a confident answer about a deleted row."""
    from jobagent.assistant.knowledge import open_index, reindex_postings

    jid = _job(store, "Doomed Engineer", score=0.1,
                description="A Doomed Engineer role at a doomed company.")
    reindex_postings(store, open_index(store))
    assert open_index(store).search("Doomed"), "fixture should be indexed first"

    store.purge_jobs(max_score=0.7, dry_run=False)
    assert store.get_job(jid) is None
    rebuilt = reindex_postings(store, open_index(store))
    assert rebuilt == 0
    assert open_index(store).search("Doomed") == [], "a deleted posting must not be retrievable"
