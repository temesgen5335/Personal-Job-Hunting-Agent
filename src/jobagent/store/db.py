"""Thin SQLite store. Stdlib-only so Phase 0 installs with zero heavy deps.

The public surface (upsert_job, get_top_matches, log_event, ...) is what MCP
tools and the bot call — swapping SQLite for Postgres later means reimplementing
this module, not touching callers.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from jobagent.core.schemas import Event, JobPosting, Match

_SCHEMA = Path(__file__).with_name("schema.sql")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")

    def init_schema(self) -> None:
        self.conn.executescript(_SCHEMA.read_text())
        self.conn.commit()

    # --- jobs -------------------------------------------------------------
    def upsert_job(self, job: JobPosting) -> str:
        """Insert or refresh a job by dedup_hash. Returns the job id.

        last_seen_at always bumps; first_seen_at is preserved across re-sightings
        so we can tell genuinely new postings from re-scrapes.
        """
        job_id = job.dedup_hash()
        now = _now()
        row = self.conn.execute("SELECT first_seen_at FROM jobs WHERE id=?", (job_id,)).fetchone()
        first_seen = row["first_seen_at"] if row else now
        self.conn.execute(
            """
            INSERT INTO jobs (id, source, source_job_id, title, company, location,
                is_remote, description, salary_text, apply_method, apply_url,
                apply_email, url, posted_at, fetched_at, tags, raw,
                first_seen_at, last_seen_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                last_seen_at=excluded.last_seen_at,
                description=excluded.description,
                salary_text=excluded.salary_text,
                apply_method=excluded.apply_method,
                apply_url=excluded.apply_url,
                apply_email=excluded.apply_email,
                url=excluded.url,
                tags=excluded.tags,
                raw=excluded.raw
            """,
            (
                job_id, _ev(job.source), job.source_job_id, job.title, job.company,
                job.location, int(job.is_remote), job.description, job.salary_text,
                _ev(job.apply_method), job.apply_url, job.apply_email, job.url,
                job.posted_at.isoformat() if job.posted_at else None,
                job.fetched_at.isoformat(), json.dumps(job.tags), json.dumps(job.raw),
                first_seen, now,
            ),
        )
        self.conn.commit()
        return job_id

    def is_new_job(self, job: JobPosting) -> bool:
        """True if this dedup_hash has never been seen before."""
        row = self.conn.execute(
            "SELECT 1 FROM jobs WHERE id=?", (job.dedup_hash(),)
        ).fetchone()
        return row is None

    def get_jobs(self, limit: int | None = None) -> list[dict]:
        q = "SELECT * FROM jobs ORDER BY last_seen_at DESC"
        if limit:
            q += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.conn.execute(q).fetchall()]

    def count_jobs(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    def stats(self) -> dict:
        by_source = {
            r["source"]: r["n"]
            for r in self.conn.execute(
                "SELECT source, COUNT(*) AS n FROM jobs GROUP BY source ORDER BY n DESC"
            )
        }
        matches = self.conn.execute("SELECT COUNT(*) AS n FROM matches").fetchone()["n"]
        strong = self.conn.execute(
            "SELECT COUNT(*) AS n FROM matches WHERE score >= 0.7"
        ).fetchone()["n"]
        last_ingest = self.conn.execute(
            "SELECT created_at FROM events WHERE kind='ingest' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "total_jobs": self.count_jobs(),
            "by_source": by_source,
            "matches": matches,
            "strong_matches": strong,
            "last_ingest": last_ingest["created_at"] if last_ingest else None,
        }

    # --- matches ----------------------------------------------------------
    def upsert_match(self, match: Match) -> None:
        self.conn.execute(
            """
            INSERT INTO matches (job_id, score, rationale, gaps, created_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET
                score=excluded.score, rationale=excluded.rationale,
                gaps=excluded.gaps, created_at=excluded.created_at
            """,
            (match.job_id, match.score, match.rationale, json.dumps(match.gaps), _now()),
        )
        self.conn.commit()

    def get_top_matches(self, limit: int = 10, min_score: float = 0.0) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT j.*, m.score, m.rationale, m.gaps
            FROM matches m JOIN jobs j ON j.id = m.job_id
            WHERE m.score >= ?
            ORDER BY m.score DESC
            LIMIT ?
            """,
            (min_score, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- events -----------------------------------------------------------
    def log_event(self, event: Event) -> None:
        self.conn.execute(
            "INSERT INTO events (kind, job_id, payload, created_at) VALUES (?,?,?,?)",
            (event.kind, event.job_id, json.dumps(event.payload), _now()),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def _ev(value) -> str:
    """Enum-or-str → str (schemas use use_enum_values, but be defensive)."""
    return value.value if hasattr(value, "value") else str(value)
