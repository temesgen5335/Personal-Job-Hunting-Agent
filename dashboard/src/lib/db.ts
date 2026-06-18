// Read-only access to the same SQLite store the Python agent writes.
// Mirrors jobagent.store queries; never mutates. Path via JOBAGENT_DB_PATH or
// the default ../data/jobagent.db (dashboard runs from the dashboard/ dir).
import Database from "better-sqlite3";
import fs from "node:fs";
import path from "node:path";

const DB_PATH = process.env.JOBAGENT_DB_PATH
  ? path.resolve(process.env.JOBAGENT_DB_PATH)
  : path.join(process.cwd(), "..", "data", "jobagent.db");

let _db: Database.Database | null = null;

export function storeExists(): boolean {
  return fs.existsSync(DB_PATH);
}
export function storePath(): string {
  return DB_PATH;
}

function db(): Database.Database {
  if (!storeExists()) throw new Error(`Store not found at ${DB_PATH}. Run the pipeline first.`);
  if (!_db) _db = new Database(DB_PATH, { readonly: true, fileMustExist: true });
  return _db;
}

export interface Stats {
  totalJobs: number;
  matches: number;
  strong: number;
  bySource: { source: string; n: number }[];
  apps: { status: string; n: number }[];
  totalApps: number;
  lastIngest: string | null;
}

export function getStats(): Stats {
  const d = db();
  const one = (sql: string) => (d.prepare(sql).get() as any) ?? {};
  return {
    totalJobs: one("SELECT COUNT(*) n FROM jobs").n ?? 0,
    matches: one("SELECT COUNT(*) n FROM matches").n ?? 0,
    strong: one("SELECT COUNT(*) n FROM matches WHERE score>=0.7").n ?? 0,
    bySource: d.prepare("SELECT source, COUNT(*) n FROM jobs GROUP BY source ORDER BY n DESC").all() as any,
    apps: d.prepare("SELECT status, COUNT(*) n FROM applications GROUP BY status ORDER BY n DESC").all() as any,
    totalApps: one("SELECT COUNT(*) n FROM applications").n ?? 0,
    lastIngest: one("SELECT created_at c FROM events WHERE kind='ingest' ORDER BY id DESC LIMIT 1").c ?? null,
  };
}

export interface MatchRow {
  id: string;
  title: string;
  company: string | null;
  location: string | null;
  is_remote: number;
  source: string;
  url: string | null;
  apply_url: string | null;
  posted_at: string | null;
  score: number;
  rationale: string;
}

export interface MatchFilter {
  days?: number;
  location?: "remote" | "hybrid" | "any";
  q?: string;
  limit?: number;
}

export function getMatches(f: MatchFilter = {}): MatchRow[] {
  const where: string[] = ["m.score >= 0"];
  const params: any[] = [];

  if (f.days && f.days > 0) {
    const cutoff = new Date(Date.now() - f.days * 86_400_000).toISOString();
    where.push("COALESCE(NULLIF(j.posted_at, ''), j.first_seen_at) >= ?");
    params.push(cutoff);
  }
  if (f.location === "remote") where.push("(j.is_remote = 1 OR LOWER(j.location) LIKE '%remote%')");
  else if (f.location === "hybrid") where.push("LOWER(j.location) LIKE '%hybrid%'");
  if (f.q) {
    where.push("(LOWER(j.title) LIKE ? OR LOWER(j.description) LIKE ? OR LOWER(j.tags) LIKE ?)");
    const k = `%${f.q.toLowerCase()}%`;
    params.push(k, k, k);
  }

  const sql =
    "SELECT j.id, j.title, j.company, j.location, j.is_remote, j.source, j.url, j.apply_url, " +
    "j.posted_at, m.score, m.rationale FROM matches m JOIN jobs j ON j.id = m.job_id WHERE " +
    where.join(" AND ") + " ORDER BY m.score DESC LIMIT ?";
  params.push(f.limit ?? 50);
  return db().prepare(sql).all(...params) as any;
}

export interface AppRow {
  id: string;
  title: string;
  company: string | null;
  status: string;
  apply_method: string;
  created_at: string;
  submitted_at: string | null;
  url: string | null;
  apply_url: string | null;
}

export function getApplications(limit = 100): AppRow[] {
  return db()
    .prepare(
      "SELECT a.id, a.status, a.apply_method, a.created_at, a.submitted_at, " +
        "j.title, j.company, j.url, j.apply_url FROM applications a " +
        "JOIN jobs j ON j.id = a.job_id ORDER BY a.created_at DESC LIMIT ?"
    )
    .all(limit) as any;
}
