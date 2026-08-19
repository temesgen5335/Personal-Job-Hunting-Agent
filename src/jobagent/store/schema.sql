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
    -- Parsed from salary_text at ingest. NULL means "could not tell", never "zero" —
    -- a filter must skip unknowns rather than read them as unpaid.
    salary_min      REAL,
    salary_max      REAL,
    salary_currency TEXT,
    salary_period   TEXT,           -- hour | day | week | month | year
    -- Groups the same role seen on several boards. Deliberately NOT the primary key:
    -- changing that would re-id every stored job and orphan every application, which
    -- docs/VERSIONING.md classes as a MAJOR. This is additive instead.
    cluster_key     TEXT,
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
-- NOTE: the index on cluster_key is created in Store._migrate(), not here. This script
-- runs against pre-existing stores too, where the column does not exist yet — indexing
-- it here failed the whole script before the migration could add it.

CREATE TABLE IF NOT EXISTS matches (
    job_id      TEXT NOT NULL REFERENCES jobs(id),
    score       REAL NOT NULL,
    rationale   TEXT NOT NULL DEFAULT '',
    gaps        TEXT NOT NULL DEFAULT '[]',   -- JSON array
    -- Which scorer produced `score`. Without this, a heuristic re-run silently replaced
    -- an LLM rerank and nothing recorded that the better number had ever existed.
    score_source TEXT NOT NULL DEFAULT 'heuristic',   -- heuristic | llm
    -- The LLM's own score, kept in its own column so a heuristic pass cannot clobber it.
    llm_score   REAL,
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
