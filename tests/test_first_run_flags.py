"""The dashboard needs to tell three states apart: brand-new (empty), showing demo data,
and a real store. These flags on stats() are that signal."""

import sys
from pathlib import Path

from jobagent.core.schemas import JobPosting, Source
from jobagent.store import Store

ROOT = Path(__file__).resolve().parent.parent


def test_stats_first_run_and_demo_flags(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    from seed_demo import seed_if_empty

    db = tmp_path / "s.db"
    s = Store(str(db)); s.init_schema()
    st = s.stats()
    assert st["first_run"] is True and st["demo"] is False   # empty store
    s.close()

    seed_if_empty(str(db), jobs=8)                            # demo-only store
    s = Store(str(db))
    st = s.stats()
    assert st["demo"] is True and st["first_run"] is True     # demo present, no real jobs
    s.upsert_job(JobPosting(source=Source.remoteok, title="Real", company="Co"))
    st = s.stats()
    assert st["first_run"] is False                           # a real job exists
    s.close()
