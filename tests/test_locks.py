"""Advisory pipeline lock (audit M5): two passes must not interleave on one store."""

from datetime import datetime, timedelta, timezone

from jobagent.store import Store


def _store(tmp_path, name="l.db"):
    s = Store(str(tmp_path / name))
    s.init_schema()
    return s


def test_acquire_then_conflict_then_release_then_reacquire(tmp_path):
    s = _store(tmp_path)
    assert s.try_acquire_lock("pipeline", "run-A") is True
    assert s.try_acquire_lock("pipeline", "run-B") is False     # held
    s.release_lock("pipeline", "run-A")
    assert s.try_acquire_lock("pipeline", "run-B") is True      # free again
    s.close()


def test_release_is_holder_scoped(tmp_path):
    """A stale process releasing 'its' lock must not free the current holder's."""
    s = _store(tmp_path)
    assert s.try_acquire_lock("pipeline", "run-A")
    s.release_lock("pipeline", "run-STALE")                     # wrong holder → no-op
    assert s.try_acquire_lock("pipeline", "run-B") is False     # A still holds it
    s.close()


def test_stale_lock_expires_after_ttl(tmp_path):
    """A crashed holder must not wedge the pipeline forever."""
    s = _store(tmp_path)
    assert s.try_acquire_lock("pipeline", "crashed-run")
    old = (datetime.now(timezone.utc) - timedelta(minutes=180)).isoformat()
    s.conn.execute("UPDATE locks SET acquired_at=? WHERE name='pipeline'", (old,))
    s.conn.commit()
    assert s.try_acquire_lock("pipeline", "new-run", ttl_minutes=120) is True
    s.close()


def test_fresh_lock_survives_the_ttl_check(tmp_path):
    s = _store(tmp_path)
    assert s.try_acquire_lock("pipeline", "run-A", ttl_minutes=120)
    assert s.try_acquire_lock("pipeline", "run-B", ttl_minutes=120) is False
    s.close()


def test_locks_are_per_name(tmp_path):
    s = _store(tmp_path)
    assert s.try_acquire_lock("pipeline", "A")
    assert s.try_acquire_lock("some-other-job", "A") is True    # independent names
    s.close()


def test_two_connections_same_db_contend(tmp_path):
    """The whole point: a second process (timer vs manual run) sees the lock."""
    s1 = _store(tmp_path, "shared.db")
    s2 = Store(str(tmp_path / "shared.db"))
    assert s1.try_acquire_lock("pipeline", "proc-1") is True
    assert s2.try_acquire_lock("pipeline", "proc-2") is False
    s1.close()
    s2.close()
