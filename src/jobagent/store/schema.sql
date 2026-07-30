-- Single source of truth for the job agent. SQLite for v1; the column shapes
-- map cleanly onto Postgres later. JSON columns hold the full source payload
-- and structured fields so we never discard data from a source.

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,          -- dedup_hash
    source          TEXT NOT NULL,
    source_job_id   TEXT,
    title           TEXT NOT NULL,
    company         TEXT,
    location        TEXT,
    is_remote       INTEGER NOT NULL DEFAULT 0,
    description     TEXT NOT NULL DEFAULT '',
    salary_text     TEXT,
    apply_method    TEXT NOT NULL DEFAULT 'unknown',
    apply_url       TEXT,
    apply_email     TEXT,
    url             TEXT,
    posted_at       TEXT,
    fetched_at      TEXT NOT NULL,
    tags            TEXT NOT NULL DEFAULT '[]', -- JSON array
    raw             TEXT NOT NULL DEFAULT '{}', -- JSON object (full payload)
    first_seen_at   TEXT NOT NULL,
    last_seen_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source);
CREATE INDEX IF NOT EXISTS idx_jobs_fetched ON jobs(fetched_at);

CREATE TABLE IF NOT EXISTS matches (
    job_id      TEXT NOT NULL REFERENCES jobs(id),
    score       REAL NOT NULL,
    rationale   TEXT NOT NULL DEFAULT '',
    gaps        TEXT NOT NULL DEFAULT '[]',   -- JSON array
    created_at  TEXT NOT NULL,
    PRIMARY KEY (job_id)
);
CREATE INDEX IF NOT EXISTS idx_matches_score ON matches(score DESC);

CREATE TABLE IF NOT EXISTS cv_variants (
    id                TEXT PRIMARY KEY,
    job_id            TEXT NOT NULL REFERENCES jobs(id),
    base_cv_id        TEXT NOT NULL,
    content_markdown  TEXT NOT NULL,
    notes             TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id              TEXT PRIMARY KEY,
    job_id          TEXT NOT NULL REFERENCES jobs(id),
    status          TEXT NOT NULL DEFAULT 'matched',
    cv_variant_id   TEXT REFERENCES cv_variants(id),
    cover_letter    TEXT,
    email_draft     TEXT,
    apply_method    TEXT NOT NULL DEFAULT 'unknown',
    approved_at     TEXT,                       -- set ONLY on explicit user approval
    submitted_at    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_apps_status ON applications(status);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    job_id      TEXT,
    payload     TEXT NOT NULL DEFAULT '{}',     -- JSON object
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);

-- Per-job triage decisions (dashboard redesign). One row per job the user acted on:
-- dismissed (hidden from the queue permanently), snoozed (hidden until a date), or
-- just annotated. state NULL + note set = an annotated, still-live job.
CREATE TABLE IF NOT EXISTS triage (
    job_id        TEXT PRIMARY KEY REFERENCES jobs(id),
    state         TEXT,                        -- 'dismissed' | 'snoozed' | NULL
    snoozed_until TEXT,                        -- ISO ts; only for state='snoozed'
    note          TEXT,
    updated_at    TEXT NOT NULL
);
