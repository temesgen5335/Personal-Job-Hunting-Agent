"""Seed a throwaway store with realistic-looking data. `make demo`.

A fresh clone has an empty store, so every dashboard page renders an empty state and the
system looks broken before it looks useful. This lets someone judge the UI in thirty
seconds, before committing any credentials to it.

Writes to `data/demo.db` — NEVER the real store. Every seeded company is fictional and
every posting is marked in its description, so demo rows can never be mistaken for real
matches, and a demo store that gets left behind is obvious rather than confusing.
"""

import argparse
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from jobagent.core.schemas import (  # noqa: E402
    Application,
    ApplyMethod,
    Event,
    JobPosting,
    Match,
    Source,
)
from jobagent.store import Store  # noqa: E402

DEMO_DB = "data/demo.db"
MARK = "[DEMO DATA — seeded by scripts/seed_demo.py, not a real posting]"

COMPANIES = [
    ("Northwind Labs", Source.greenhouse), ("Kestrel Systems", Source.lever),
    ("Tessellate", Source.ashby), ("Blue Harbour", Source.remoteok),
    ("Meridian Data", Source.remotive), ("Ольха Tech", Source.greenhouse),
    ("Fernwood AI", Source.ashby), ("Kite & Co", Source.lever),
]
ROLES = [
    ("Senior Backend Engineer", 0.88), ("Full-Stack Engineer (Remote)", 0.81),
    ("AI Engineer — Agents", 0.93), ("Platform Engineer", 0.74),
    ("Frontend Engineer, Design Systems", 0.69), ("Data Engineer", 0.55),
    ("Engineering Manager", 0.32), ("Sales Engineer", 0.21),
    ("Staff Software Engineer", 0.79), ("Junior QA Analyst", 0.18),
]
STACKS = ["Python, FastAPI, Postgres", "TypeScript, React, Node",
          "Go, Kubernetes, gRPC", "Python, LangGraph, vector search"]


def seed(db_path: str, *, jobs: int, seed_value: int = 7) -> dict:
    rng = random.Random(seed_value)          # deterministic: two demos look the same
    store = Store(db_path)
    store.init_schema()
    now = datetime.now(timezone.utc)

    made = []
    for i in range(jobs):
        company, source = COMPANIES[i % len(COMPANIES)]
        title, base = ROLES[i % len(ROLES)]
        score = max(0.05, min(0.97, base + rng.uniform(-0.08, 0.08)))
        posted = now - timedelta(days=rng.randint(0, 21), hours=rng.randint(0, 23))
        job = JobPosting(
            source=source,
            source_job_id=f"demo-{i}",
            title=title,
            company=company,
            location=rng.choice(["Remote", "Remote (EMEA)", "Remote (Global)", "Hybrid — Berlin"]),
            is_remote=True,
            url=f"https://example.com/demo/{i}",
            apply_url=f"https://example.com/demo/{i}/apply",
            apply_method=ApplyMethod.ats_form if i % 3 else ApplyMethod.email,
            apply_email="jobs@example.com" if i % 3 == 0 else None,
            description=f"{MARK}\n\nWe are hiring a {title}. Stack: {rng.choice(STACKS)}. "
                        f"Fully remote, async-friendly team.",
            posted_at=posted.isoformat(),
            salary_text=rng.choice(["", "$120k–$160k", "€90k–€120k", ""]),
        )
        jid = store.upsert_job(job)
        store.upsert_match(Match(
            job_id=jid, score=round(score, 3),
            rationale=f"demo match — skills: {rng.choice(STACKS)}; role tier: direct",
        ))
        made.append((jid, title, company))

    # A couple of applications so the tracker and funnel have shape.
    for jid, title, company in made[:3]:
        store.create_application(Application(
            job_id=jid, apply_method=ApplyMethod.email, status="submitted",
            submitted_at=(now - timedelta(days=rng.randint(3, 14))).isoformat(),
        ))
    # And one triaged row so the queue is not the only thing on the page.
    if len(made) > 4:
        store.set_triage(made[4][0], state="dismissed")

    # An ingest event, or the dashboard reports the pipeline as never having run.
    for source in {s for _, s in COMPANIES}:
        store.log_event(Event(kind="ingest", payload={
            "source": source.value, "fetched": jobs // 4, "new": jobs // 8,
            "kept": jobs // 4, "dropped": 0, "drops": {}, "run_id": "demo-run",
        }))
    store.log_event(Event(kind="match", payload={
        "scored": jobs, "llm_reranked": 0, "used_llm": False, "run_id": "demo-run"}))

    stats = store.stats()
    store.close()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed a demo store to explore the UI.")
    ap.add_argument("--db", default=DEMO_DB, help=f"target store (default {DEMO_DB})")
    ap.add_argument("--jobs", type=int, default=40)
    ap.add_argument("--force", action="store_true",
                    help="allow writing to a store that already has jobs")
    args = ap.parse_args()

    target = Path(args.db)
    if target.exists() and not args.force:
        existing = Store(str(target))
        try:
            n = existing.count_jobs()
        finally:
            existing.close()
        if n:
            print(f"❌ {target} already has {n:,} jobs. Demo data would mix into it.")
            print("   Use --db data/demo.db (the default), or --force if you are sure.")
            raise SystemExit(1)

    target.parent.mkdir(parents=True, exist_ok=True)
    stats = seed(str(target), jobs=args.jobs)
    print(f"✅ seeded {stats['total_jobs']} jobs, {stats['strong_matches']} strong, "
          f"{stats['total_apps']} applications → {target}")
    print()
    print("  Explore it without touching your real store:")
    print(f"    JOBAGENT_DB_PATH={target} make run")
    print()
    print(f"  Every posting is fictional and marked in its description. Delete with:")
    print(f"    rm {target}")


if __name__ == "__main__":
    main()
