"""Phase 0 smoke tests: schemas validate, dedup is stable, store round-trips."""

from jobagent.core.schemas import (
    ApplicationStatus,
    JobPosting,
    Match,
    Source,
)
from jobagent.store import Store


def test_dedup_hash_stable_and_source_independent():
    a = JobPosting(source=Source.remoteok, title="AI Engineer", company="Acme", location="Remote")
    b = JobPosting(source=Source.telegram, title="ai  engineer", company="ACME", location="remote")
    # Same role from two sources collapses to one logical job.
    assert a.dedup_hash() == b.dedup_hash()

    c = JobPosting(source=Source.remoteok, title="Backend Engineer", company="Acme")
    assert a.dedup_hash() != c.dedup_hash()


def test_store_roundtrip(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()

    job = JobPosting(
        source=Source.remotive, title="ML Engineer", company="Globex", is_remote=True
    )
    assert store.is_new_job(job) is True
    job_id = store.upsert_job(job)
    assert store.is_new_job(job) is False

    store.upsert_match(Match(job_id=job_id, score=0.87, rationale="strong fit"))
    top = store.get_top_matches(limit=5, min_score=0.5)
    assert len(top) == 1
    assert top[0]["title"] == "ML Engineer"
    assert top[0]["score"] == 0.87
    store.close()


def test_application_status_enum():
    assert ApplicationStatus.awaiting_approval.value == "awaiting_approval"


# --- M4: status transition map ----------------------------------------------------

def test_transition_map_is_mostly_forward():
    from jobagent.core.schemas import allowed_next, can_transition

    assert can_transition("matched", "drafting")
    assert can_transition("awaiting_approval", "submitted")
    assert can_transition("submitted", "interview")
    assert can_transition("interview", "offer")
    # Nonsense moves that would corrupt the funnel analytics.
    assert not can_transition("offer", "matched")
    assert not can_transition("submitted", "drafting")     # cannot un-send
    assert not can_transition("awaiting_approval", "interview")   # nothing was sent
    assert allowed_next("rejected") == set()               # terminal


def test_same_status_is_a_permitted_noop():
    from jobagent.core.schemas import can_transition
    for s in ("matched", "submitted", "rejected"):
        assert can_transition(s, s), s


def test_recoverable_states_have_a_way_back():
    """A skipped job can be reconsidered and a failed automation retried —
    enforcement must not create dead ends for states that aren't outcomes."""
    from jobagent.core.schemas import allowed_next
    assert "matched" in allowed_next("skipped")
    assert "drafting" in allowed_next("failed")


def test_every_status_is_in_the_map():
    """A new ApplicationStatus without a transition entry would silently become a
    dead end (allowed_next returns empty), so require an explicit decision."""
    from jobagent.core.schemas import ALLOWED_TRANSITIONS, ApplicationStatus
    assert {s.value for s in ApplicationStatus} == set(ALLOWED_TRANSITIONS)


def test_transition_targets_are_real_statuses():
    from jobagent.core.schemas import ALLOWED_TRANSITIONS, ApplicationStatus
    valid = {s.value for s in ApplicationStatus}
    for src, targets in ALLOWED_TRANSITIONS.items():
        assert targets <= valid, f"{src} points at unknown status(es): {targets - valid}"
