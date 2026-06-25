// Dashboard data layer — talks to the FastAPI orchestrator (v2), not SQLite directly.
// Set JOBAGENT_API_URL to point at the backend (default http://127.0.0.1:8077).
const API = process.env.JOBAGENT_API_URL || "http://127.0.0.1:8077";

export function apiBase(): string {
  return API;
}

async function getJSON(path: string): Promise<any> {
  const res = await fetch(`${API}${path}`);
  if (!res.ok) throw new Error(`API ${res.status} ${res.statusText} for ${path}`);
  return res.json();
}

export interface Stats {
  totalJobs: number;
  matches: number;
  strong: number;
  totalApps: number;
  lastIngest: string | null;
  bySource: { source: string; n: number }[];
  apps: { status: string; n: number }[];
}

export async function getStats(): Promise<Stats> {
  const s = await getJSON("/stats");
  return {
    totalJobs: s.total_jobs ?? 0,
    matches: s.matches ?? 0,
    strong: s.strong_matches ?? 0,
    totalApps: s.total_apps ?? 0,
    lastIngest: s.last_ingest ?? null,
    bySource: Object.entries(s.by_source ?? {}).map(([source, n]) => ({ source, n: n as number })),
    apps: s.apps ?? [],
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

export async function getMatches(f: MatchFilter = {}): Promise<MatchRow[]> {
  const p = new URLSearchParams();
  if (f.days) p.set("days", String(f.days));
  if (f.location) p.set("location", f.location);
  if (f.q) p.set("q", f.q);
  p.set("limit", String(f.limit ?? 50));
  return (await getJSON(`/jobs?${p.toString()}`)).jobs;
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

export async function getApplications(limit = 200): Promise<AppRow[]> {
  return (await getJSON(`/applications?limit=${limit}`)).applications;
}

export interface JobDetail extends MatchRow {
  description?: string;
  salary_text?: string | null;
  apply_email?: string | null;
  tags?: string;
}

export async function getJob(id: string): Promise<JobDetail> {
  return getJSON(`/job/${encodeURIComponent(id)}`);
}

export interface Analytics {
  total: number;
  by_status: Record<string, number>;
  by_source: { source: string; n: number }[];
  timeline: { day: string; n: number }[];
  submitted: number;
  interview: number;
  offer: number;
  rejected: number;
  response_rate: number;
  interview_rate: number;
  offer_rate: number;
}

export async function getAnalytics(): Promise<Analytics> {
  return getJSON("/analytics");
}

export const APPLICATION_STATUSES = [
  "matched", "drafting", "awaiting_approval", "submitted",
  "rejected", "interview", "offer", "skipped", "failed",
];

