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

    def prune_jobs(self, *, older_than_days: int, vacuum: bool = False) -> dict:
        """Drop stale postings to bound store growth. Returns what was removed.

        Retention, not filtering — distinct from the ingest gate (R4a), which decides
        what to store in the first place. A job board posting is dead within weeks, so
        keeping every one forever costs disk and matching time for nothing.

        Two hard rules:
        - **Anything you acted on is never pruned.** Jobs referenced by `applications`
          or `cv_variants` are kept regardless of age; that is your own history, and
          deleting it would orphan the application record (and violate the FK).
        - Dependent `matches`/`triage` rows are removed first, because
          `PRAGMA foreign_keys = ON` would otherwise reject the delete.

        `last_seen_at` is the age basis, not `posted_at`: a posting still appearing in
        a feed today is live even if it was first published months ago.
        """
        self._ensure_triage()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        # Computed before the early return: "stale but spared" is the interesting
        # number even on a pass that deletes nothing.
        kept = self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM jobs
            WHERE last_seen_at < ?
              AND (id IN (SELECT job_id FROM applications)
                   OR id IN (SELECT job_id FROM cv_variants))
            """,
            (cutoff,),
        ).fetchone()["n"]

        doomed = [
            r["id"] for r in self.conn.execute(
                """
                SELECT id FROM jobs
                WHERE last_seen_at < ?
                  AND id NOT IN (SELECT job_id FROM applications)
                  AND id NOT IN (SELECT job_id FROM cv_variants)
                """,
                (cutoff,),
            )
        ]
        if not doomed:
            return {"jobs": 0, "matches": 0, "triage": 0, "kept_acted_on": kept}

        marks = ",".join("?" for _ in doomed)
        matches = self.conn.execute(
            f"DELETE FROM matches WHERE job_id IN ({marks})", doomed).rowcount
        triage = self.conn.execute(
            f"DELETE FROM triage WHERE job_id IN ({marks})", doomed).rowcount
        jobs = self.conn.execute(
            f"DELETE FROM jobs WHERE id IN ({marks})", doomed).rowcount
        self.conn.commit()

        if vacuum:
            # Reclaims the file space DELETE only marks free. Rewrites the whole db,
            # so it is opt-in rather than automatic.
            self.conn.execute("VACUUM")
        return {"jobs": jobs, "matches": matches, "triage": triage, "kept_acted_on": kept}

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
        self._ensure_triage()
        # The triage queue: strong matches with no live decision. This is the number
        # on the Jobs nav badge and the "not yet triaged" stat — the reason the
        # dashboard gets opened in the morning.
        queue = self.conn.execute(
            "SELECT COUNT(*) AS n FROM matches m LEFT JOIN triage t ON t.job_id = m.job_id "
            "WHERE m.score >= 0.7 AND (t.state IS NULL "
            "  OR (t.state='snoozed' AND COALESCE(t.snoozed_until,'') <= ?))",
            (_now(),),
        ).fetchone()["n"]
        return {
            "total_jobs": self.count_jobs(),
            "by_source": by_source,
            "matches": matches,
            "strong_matches": strong,
            "queue": queue,
            "last_ingest": last_ingest["created_at"] if last_ingest else None,
            "apps": apps,
            "total_apps": sum(a["n"] for a in apps),
            "health": self.pipeline_health(),
        }

    def pipeline_health(self, *, stale_after_hours: float = 24.0, error_window_hours: float = 24.0) -> dict:
        """Is the agent actually still running?

        Without this, a pipeline that has been failing for three days renders
        identically to a healthy one — the store just stops growing and nothing says
        so. Surfaces staleness, a recent-error count, the last error, and per-source
        freshness, all derived from the append-only `events` trail.
        """
        now = datetime.now(timezone.utc)

        def _age_hours(ts: str | None) -> float | None:
            if not ts:
                return None
            try:
                return max(0.0, (now - datetime.fromisoformat(ts)).total_seconds() / 3600.0)
            except ValueError:
                return None

        last_ingest = self.conn.execute(
            "SELECT created_at FROM events WHERE kind='ingest' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_at = last_ingest["created_at"] if last_ingest else None
        age = _age_hours(last_at)

        cutoff = (now - timedelta(hours=error_window_hours)).isoformat()
        recent_errors = self.conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE kind='error' AND created_at >= ?", (cutoff,)
        ).fetchone()["n"]

        err = self.conn.execute(
            "SELECT payload, created_at FROM events WHERE kind='error' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last_error = None
        if err:
            try:
                payload = json.loads(err["payload"])
            except (ValueError, TypeError):
                payload = {}
            last_error = {
                "source": payload.get("source"),
                "error": payload.get("error"),
                "at": err["created_at"],
            }

        # Most recent ingest event per source — one row each, newest first.
        sources = [
            {
                "source": r["source"],
                "last_ingest": r["created_at"],
                "hours_since": _age_hours(r["created_at"]),
                "fetched": (json.loads(r["payload"]) or {}).get("fetched"),
                "new": (json.loads(r["payload"]) or {}).get("new"),
            }
            for r in self.conn.execute(
                """
                SELECT source, payload, created_at FROM (
                    SELECT json_extract(payload, '$.source') AS source, payload, created_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY json_extract(payload, '$.source') ORDER BY id DESC
                           ) AS rn
                    FROM events WHERE kind='ingest'
                ) WHERE rn = 1 ORDER BY source
                """
            )
            if r["source"]
        ]

        return {
            "last_ingest": last_at,
            "hours_since_ingest": age,
            # No ingest ever recorded also counts as stale — a fresh install and a
            # three-day-dead pipeline should both read as "not currently working".
            "is_stale": age is None or age > stale_after_hours,
            "stale_after_hours": stale_after_hours,
            "recent_errors": recent_errors,
            "last_error": last_error,
            "sources": sources,
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
        exclude_locations: list[str] | None = None,
        include_locations: list[str] | None = None,
        sources: list[str] | None = None,
        hide_triaged: bool = False,
        offset: int = 0,
    ) -> list[dict]:
        """Ranked matches with filters: recency, location mode (remote/hybrid/any),
        keyword OR-match, exclude_locations (drop), include_locations (keep-only),
        sources (keep-only, for the dashboard's per-source visibility toggle),
        hide_triaged (drop jobs you already dismissed or snoozed), and pagination
        via offset.

        `hide_triaged` exists because two consumers want opposite things: a shortlist
        for action (digest, bot, /apply) must not re-offer what you already decided,
        while the dashboard deliberately shows those rows so it can render Undo."""
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

        for loc in exclude_locations or []:
            where.append("LOWER(COALESCE(j.location,'')) NOT LIKE ?")
            params.append(f"%{loc.lower()}%")

        if include_locations:
            ors = []
            for loc in include_locations:
                ors.append("LOWER(COALESCE(j.location,'')) LIKE ?")
                params.append(f"%{loc.lower()}%")
            where.append("(" + " OR ".join(ors) + ")")

        if sources:
            # Exact match on the source slug — not LIKE, so "lever" can never also
            # select a future "lever-eu".
            where.append("j.source IN (" + ",".join("?" for _ in sources) + ")")
            params += [s.lower() for s in sources]

        if hide_triaged:
            # Same lapsed-snooze predicate stats() uses for the queue count, so the
            # badge and the digest can never disagree about what is still live. A
            # note-only row (state NULL) stays visible — annotating is not deciding.
            where.append("(t.state IS NULL OR "
                         " (t.state='snoozed' AND COALESCE(t.snoozed_until,'') <= ?))")
            params.append(_now())

        self._ensure_triage()
        sql = (
            "SELECT j.*, m.score, m.rationale, m.gaps, "
            # Lapsed snoozes read as live directly in SQL so ordering and the
            # dashboard queue agree with get_triage()'s view of the world.
            "CASE WHEN t.state='snoozed' AND COALESCE(t.snoozed_until,'') <= ? THEN NULL "
            "     ELSE t.state END AS triage_state, "
            "t.snoozed_until AS triage_snoozed_until, t.note AS triage_note "
            "FROM matches m JOIN jobs j ON j.id = m.job_id "
            "LEFT JOIN triage t ON t.job_id = j.id "
            "WHERE " + " AND ".join(where)
            + " ORDER BY m.score DESC LIMIT ? OFFSET ?"
        )
        params = [_now()] + params + [limit, offset]
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

    def applications_needing_followup(self, *, after_days: int = 7, limit: int = 50) -> list[dict]:
        """Submitted applications that have gone quiet and deserve a nudge.

        A follow-up logged inside the same window suppresses the reminder, so the list
        renews itself after another `after_days` instead of either nagging every run or
        going silent forever after one draft.
        """
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=after_days)).isoformat()
        rows = self.conn.execute(
            """
            SELECT a.id, a.status, a.submitted_at, j.title, j.company,
                   j.apply_email, j.url, j.apply_url
            FROM applications a
            JOIN jobs j ON j.id = a.job_id
            WHERE a.status = 'submitted'
              AND a.submitted_at IS NOT NULL
              AND a.submitted_at <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM events e
                  WHERE e.kind = 'followup_drafted'
                    AND json_extract(e.payload, '$.application_id') = a.id
                    AND e.created_at > ?
              )
            ORDER BY a.submitted_at ASC
            LIMIT ?
            """,
            (cutoff, cutoff, limit),
        ).fetchall()

        out = []
        for r in rows:
            row = dict(r)
            try:
                waited = (now - datetime.fromisoformat(row["submitted_at"])).days
            except (ValueError, TypeError):
                waited = after_days
            row["days_waiting"] = max(0, waited)
            out.append(row)
        return out

    # --- triage (dismiss / snooze / note) -----------------------------------
    # The dashboard's queue is "strong matches you haven't decided on yet", so a
    # decision has to be stored somewhere the next page load can see. Snoozes carry
    # an expiry and lapse back to live on read — nothing re-arms them manually.

    def _ensure_triage(self) -> None:
        # Existing databases predate the table; keep reads/writes self-healing.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS triage (job_id TEXT PRIMARY KEY REFERENCES jobs(id), "
            "state TEXT, snoozed_until TEXT, note TEXT, updated_at TEXT NOT NULL)"
        )

    _KEEP = object()   # sentinel: "field not provided — keep what's stored"

    def set_triage(self, job_id: str, *, state=_KEEP, snoozed_until=_KEEP, note=_KEEP) -> dict:
        """Upsert one job's triage row. Omitted fields keep their stored value, so
        noting a snoozed job keeps the snooze and re-snoozing keeps the note. Pass
        None explicitly to clear a field."""
        self._ensure_triage()
        existing = self.get_triage(job_id) or {}
        merged_state = existing.get("state") if state is self._KEEP else state
        merged_until = existing.get("snoozed_until") if snoozed_until is self._KEEP else snoozed_until
        merged_note = existing.get("note") if note is self._KEEP else note
        self.conn.execute(
            "INSERT INTO triage (job_id, state, snoozed_until, note, updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
            "state=excluded.state, snoozed_until=excluded.snoozed_until, "
            "note=excluded.note, updated_at=excluded.updated_at",
            (job_id, merged_state, merged_until, merged_note, _now()),
        )
        self.conn.commit()
        return self.get_triage(job_id) or {}

    def clear_triage(self, job_id: str) -> None:
        """Undo: back to live. The note survives — undoing a dismissal shouldn't
        delete what you wrote about the job."""
        self._ensure_triage()
        row = self.get_triage(job_id)
        if row and row.get("note"):
            self.set_triage(job_id, state=None, snoozed_until=None)
        else:
            self.conn.execute("DELETE FROM triage WHERE job_id=?", (job_id,))
            self.conn.commit()

    def get_triage(self, job_id: str) -> dict | None:
        self._ensure_triage()
        row = self.conn.execute("SELECT * FROM triage WHERE job_id=?", (job_id,)).fetchone()
        if not row:
            return None
        out = dict(row)
        # A lapsed snooze reads as live. Report it as such rather than making every
        # caller re-derive "snoozed but expired".
        if out.get("state") == "snoozed" and (out.get("snoozed_until") or "") <= _now():
            out["state"] = None
            out["snoozed_until"] = None
        return out

    # --- advisory locks ----------------------------------------------------
    # Guards against two pipeline passes interleaving on one store (audit M5):
    # a timer firing while a manual `make pipeline` or POST /ingest is mid-run
    # would double-fetch sources and interleave ledger events. SQLite's PRIMARY
    # KEY makes acquisition atomic; the TTL means a crashed holder expires
    # instead of wedging the pipeline until someone notices.

    def try_acquire_lock(self, name: str, holder: str, *, ttl_minutes: float = 120.0) -> bool:
        """Atomically acquire a named lock. False if a live holder already has it."""
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS locks (name TEXT PRIMARY KEY, holder TEXT NOT NULL, acquired_at TEXT NOT NULL)"
        )
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=ttl_minutes)).isoformat()
        self.conn.execute("DELETE FROM locks WHERE name=? AND acquired_at < ?", (name, cutoff))
        try:
            self.conn.execute(
                "INSERT INTO locks (name, holder, acquired_at) VALUES (?,?,?)",
                (name, holder, _now()),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            self.conn.commit()
            return False

    def release_lock(self, name: str, holder: str) -> None:
        """Release only your own lock — a stale holder must not free a newer one's."""
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS locks (name TEXT PRIMARY KEY, holder TEXT NOT NULL, acquired_at TEXT NOT NULL)"
        )
        self.conn.execute("DELETE FROM locks WHERE name=? AND holder=?", (name, holder))
        self.conn.commit()

    def list_runs(self, limit: int = 20, *, kind_detail: str | None = None) -> list[dict]:
        """The run ledger: one row per pipeline pass, newest first.

        Reads the `run` summary events the pipeline logs at the end of each pass.
        This is the answer to "what has the agent actually done lately" — counts per
        stage, digest outcome, duration — without grepping journald.

        Assistant sessions also close with a `run` event, so they share the audit spine
        and need no new table. They are **excluded here by default**: they carry no
        ingest or match counts, so mixing them in puts blank rows in the ledger and
        anything rendering counts prints None. Pass `kind_detail="agent_session"` to
        list those instead.
        """
        wanted = kind_detail or ""
        rows = self.conn.execute(
            "SELECT payload, created_at FROM events WHERE kind='run' "
            "AND COALESCE(json_extract(payload, '$.kind_detail'), '') = ? "
            "ORDER BY id DESC LIMIT ?",
            (wanted, limit),
        ).fetchall()
        out = []
        for r in rows:
            try:
                payload = json.loads(r["payload"])
            except (ValueError, TypeError):
                payload = {}
            payload["finished_at"] = r["created_at"]
            out.append(payload)
        return out

    def events_for_run(self, run_id: str) -> list[dict]:
        """Every event a single pass emitted, oldest first — the reconstruction view."""
        rows = self.conn.execute(
            """
            SELECT kind, job_id, payload, created_at FROM events
            WHERE json_extract(payload, '$.run_id') = ? ORDER BY id ASC
            """,
            (run_id,),
        ).fetchall()
        out = []
        for r in rows:
            try:
                payload = json.loads(r["payload"])
            except (ValueError, TypeError):
                payload = {}
            out.append({"kind": r["kind"], "job_id": r["job_id"],
                        "created_at": r["created_at"], **payload})
        return out

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
