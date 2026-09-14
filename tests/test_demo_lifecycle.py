"""clear_demo removes the seeded demo rows (which purge_jobs cannot, because it spares any
job with an application) while leaving real data untouched. The safety invariant — never
touch a non-demo row — is the point of these tests."""

import sys
from pathlib import Path

from jobagent.core.schemas import Application, ApplyMethod, JobPosting, Source
from jobagent.store import Store

ROOT = Path(__file__).resolve().parent.parent


def test_clear_demo_removes_demo_rows_and_their_dependents(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    from seed_demo import seed_if_empty

    db = tmp_path / "s.db"
    seed_if_empty(str(db), jobs=40)          # 40 demo jobs + 3 apps + 1 triage + matches
    s = Store(str(db))
    try:
        cleared = s.clear_demo()
        assert cleared["jobs"] == 40
        assert cleared["applications"] == 3
        assert cleared["matches"] == 40
        assert cleared["triage"] == 1
        st = s.stats()
        assert st["total_jobs"] == 0 and st["demo"] is False and st["first_run"] is True
        assert s.list_applications() == []   # seeded demo applications gone
    finally:
        s.close()


def test_clear_demo_never_touches_real_rows(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    from seed_demo import seed_if_empty

    db = tmp_path / "s.db"
    seed_if_empty(str(db), jobs=12)
    s = Store(str(db))
    try:
        real_id = s.upsert_job(JobPosting(source=Source.remoteok, title="Real", company="RealCo"))
        s.create_application(Application(job_id=real_id, apply_method=ApplyMethod.email, status="submitted"))
        s.clear_demo()
        assert s.count_jobs() == 1                          # only the real job remains
        apps = s.list_applications()
        assert len(apps) == 1 and apps[0]["job_id"] == real_id   # real application survived
    finally:
        s.close()
