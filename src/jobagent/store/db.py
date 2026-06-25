"""Thin SQLite store. Stdlib-only so Phase 0 installs with zero heavy deps.

The public surface (upsert_job, get_top_matches, log_event, ...) is what MCP
tools and the bot call — swapping SQLite for Postgres later means reimplementing
this module, not touching callers.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
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
        apps = [
            {"status": r["status"], "n": r["n"]}
            for r in self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM applications GROUP BY status ORDER BY n DESC"
            )
        ]
        return {
            "total_jobs": self.count_jobs(),
            "by_source": by_source,
            "matches": matches,
            "strong_matches": strong,
            "last_ingest": last_ingest["created_at"] if last_ingest else None,
            "apps": apps,
            "total_apps": sum(a["n"] for a in apps),
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

    def get_matches(
        self,
        limit: int = 10,
        min_score: float = 0.0,
        max_age_days: int | None = None,
        location: str = "any",
        keywords: list[str] | None = None,
    ) -> list[dict]:
        """Ranked matches with optional filters: recency (posted_at→first_seen_at
        fallback), location (remote/hybrid/any), keyword OR-match on title/desc/tags."""
        where = ["m.score >= ?"]
        params: list = [min_score]

        if max_age_days:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
            where.append("COALESCE(NULLIF(j.posted_at, ''), j.first_seen_at) >= ?")
            params.append(cutoff)

        if location == "remote":
            where.append("(j.is_remote = 1 OR LOWER(j.location) LIKE '%remote%')")
        elif location == "hybrid":
            where.append("LOWER(j.location) LIKE '%hybrid%'")

        if keywords:
            ors = []
            for kw in keywords:
                ors.append("(LOWER(j.title) LIKE ? OR LOWER(j.description) LIKE ? OR LOWER(j.tags) LIKE ?)")
                k = f"%{kw.lower()}%"
                params += [k, k, k]
            where.append("(" + " OR ".join(ors) + ")")

        sql = (
            "SELECT j.*, m.score, m.rationale, m.gaps FROM matches m "
            "JOIN jobs j ON j.id = m.job_id WHERE " + " AND ".join(where)
            + " ORDER BY m.score DESC LIMIT ?"
        )
        params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def get_job(self, job_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def get_match(self, job_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT score, rationale, gaps FROM matches WHERE job_id=?", (job_id,)
        ).fetchone()
        return dict(row) if row else None

    def application_analytics(self, days: int = 30) -> dict:
        """Funnel, outcome rates, by-source, and a daily timeline for the dashboard."""
        by_status = {
            r["status"]: r["n"]
            for r in self.conn.execute("SELECT status, COUNT(*) n FROM applications GROUP BY status")
        }
        by_source = [
            {"source": r["source"], "n": r["n"]}
            for r in self.conn.execute(
                "SELECT j.source, COUNT(*) n FROM applications a JOIN jobs j ON j.id=a.job_id "
                "GROUP BY j.source ORDER BY n DESC"
            )
        ]
        timeline = [
            {"day": r["d"], "n": r["n"]}
            for r in self.conn.execute(
                "SELECT substr(created_at,1,10) d, COUNT(*) n FROM applications "
                "GROUP BY d ORDER BY d DESC LIMIT ?",
                (days,),
            )
        ]
        submitted = by_status.get("submitted", 0)
        interview = by_status.get("interview", 0)
        offer = by_status.get("offer", 0)
        rejected = by_status.get("rejected", 0)
        rate = lambda n: round(n / submitted, 3) if submitted else 0.0  # noqa: E731
        return {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "by_source": by_source,
            "timeline": timeline,
            "submitted": submitted,
            "interview": interview,
            "offer": offer,
            "rejected": rejected,
            "response_rate": rate(interview + offer + rejected),
            "interview_rate": rate(interview),
            "offer_rate": rate(offer),
        }

    def list_applications(self, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            "SELECT a.id, a.status, a.apply_method, a.created_at, a.submitted_at, "
            "j.title, j.company, j.url, j.apply_url FROM applications a "
            "JOIN jobs j ON j.id = a.job_id ORDER BY a.created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- applications + cv variants --------------------------------------
    def insert_cv_variant(self, cv) -> str:
        cv_id = cv.id or uuid.uuid4().hex[:16]
        self.conn.execute(
            "INSERT INTO cv_variants (id, job_id, base_cv_id, content_markdown, notes, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (cv_id, cv.job_id, cv.base_cv_id, cv.content_markdown, cv.notes, _now()),
        )
        self.conn.commit()
        return cv_id

    def create_application(self, app) -> str:
        app_id = app.id or uuid.uuid4().hex[:16]
        now = _now()
        self.conn.execute(
            "INSERT INTO applications (id, job_id, status, cv_variant_id, cover_letter, "
            "email_draft, apply_method, approved_at, submitted_at, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                app_id, app.job_id, _ev(app.status), app.cv_variant_id, app.cover_letter,
                app.email_draft, _ev(app.apply_method), None, None, now, now,
            ),
        )
        self.conn.commit()
        return app_id

    def get_application(self, app_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        return dict(row) if row else None

    def update_application(self, app_id: str, **fields) -> None:
        allowed = {"status", "cv_variant_id", "cover_letter", "email_draft",
                   "apply_method", "approved_at", "submitted_at"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        cols = ", ".join(f"{k}=?" for k in sets)
        self.conn.execute(
            f"UPDATE applications SET {cols}, updated_at=? WHERE id=?",
            (*sets.values(), _now(), app_id),
        )
        self.conn.commit()

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
