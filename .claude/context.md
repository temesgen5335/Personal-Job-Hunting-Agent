# Context — Personal Job Agent

## Problem Statement

Job hunting as a software engineer is a high-volume, repetitive, multi-platform grind.
You monitor dozens of Telegram channels and job boards, manually cross-reference each
posting against your skills and preferences, tailor CVs and cover letters per company,
fill out near-identical ATS forms, and track outcomes in a spreadsheet. The process
scales linearly with effort and burns hours that should go toward interview prep.

This project automates every step except the final human decision: **apply or skip**.

## Vision

A self-hosted, single-user autonomous agent that:

1. **Ingests** job postings from Telegram channels, remote job boards (RemoteOK,
   Remotive), and ATS platforms (Greenhouse, Lever, Ashby) on a schedule.
2. **Matches** them against a structured profile (skills, roles, experience,
   location preferences) using a heuristic pre-filter (always, no API cost) plus
   an optional LLM rerank for the top candidates.
3. **Surfaces** ranked results in a Telegram bot with interactive filters
   (date/location/keywords) and one-tap apply buttons.
4. **Generates** tailored application assets (CV variant, cover letter, email draft)
   using multi-provider LLM failover — never fabricating experience.
5. **Fills** ATS forms with a HITL browser-automation flow (Playwright), pausing
   for human approval before any submit.
6. **Tracks** applications through a lifecycle (matched → drafted → submitted →
   interview → offer/rejected) on a self-hosted dashboard.

The system is **reusable by anyone** — write your own `config/preferences.local.toml`
(identity) and `.env` (credentials), adjust `config/preferences.toml` (target roles,
skills, watchlist), and it works for a different person. No identity is hard-coded,
and the committed config carries placeholders only.

## End Product

Two interfaces, one backend:

- **Telegram bot** — primary daily interaction. `/menu` with filters, `/jobs`,
  `/apply <rank>` with fit-check + approval gate, `/status`.
- **Astro SSR dashboard** — analytics (funnel, rates, timeline), job detail with
  on-demand fit breakdown, application status editing, LLM/Telegram/SMTP
  configuration via an auth-gated settings page.
- **FastAPI orchestrator** — sole backend. Wraps the service layer (ingestion,
  matching, apply, fit, LLM, secrets). Both bot and dashboard are its clients.

## Current State (v2 complete, July 2026)

| Component | Status |
|---|---|
| Ingestion (6 adapters) | Done — RemoteOK, Remotive, Greenhouse, Lever, Ashby, Telegram |
| Matching (heuristic + LLM) | Done — word-boundary scoring + optional LLM rerank |
| Telegram bot | Done — interactive /menu, /apply with fit-check, ATS path |
| Tier-1 apply (email) | Done — CV tailor + cover letter + SMTP send with HITL gate |
| Tier-2 apply (ATS form-fill) | Done — Playwright fill + screenshot preview + submit gate |
| FastAPI orchestrator | Done — 15+ endpoints, auth, injectable deps |
| Dashboard | Done — overview analytics, filtered job list, job detail + fit, settings |
| Multi-LLM failover | Done — Groq/OpenRouter/Gemini/OpenAI/Anthropic + custom endpoint |
| Encrypted config UI | Done — Fernet secret store, auth-gated API, masked reads |
| API auth on writes | Done (Tier 1) — bearer token on every non-GET route, fails closed |
| Pipeline health | Done (Tier 1) — staleness + error count + per-source freshness, dashboard banner, digest warnings |
| Source retry/backoff | Done (Tier 1) — bounded jittered backoff, capped Retry-After |
| PII split | Done (Tier 1) — identity in gitignored overlay; committed config is placeholders |
| History scrub | Done (2026-07-30) — CV blob + phone/email removed from all 37 commits via filter-repo |
| Status lifecycle | Done (Tier 2) — transition map enforced, audited correction override |
| Weighted matching | Done (Tier 2) — skill weights, role-zone tiers, seniority + must-have checks |
| Gap surfacing | Done (Tier 2) — gaps as chips in the dashboard job list |
| Follow-up reminders | Done (Tier 2) — quiet-application list + drafted nudges (never sent) |
| Run-ID spine | Done (Tier 3) — one id ingest→match→digest, run ledger, GET /runs + /runs/{id} |
| Matching eval harness | Done (Tier 3) — 24 labeled traps, P@5=1.0 / P@10≥0.9 floors, tuning CLI |
| Architecture doc | Done (Tier 3) — rewritten around the real system; legacy Hermes diagram gone |
| Ingest lock (M5) | Done — SQLite advisory lock w/ 2h TTL; second pass exits, API returns 409 |
| CI | Done — tests.yml runs the offline suite + F821 gate on every push/PR |
| Dependability (Phase 0) | Actions workflow hardened: ingest gate applied in CI, store pruned+vacuumed so the 10GB cache limit can't be hit, failure notifies Telegram. Silence alarm warns when a schedule was skipped. **Still needs repo secrets set to actually run.** |
| Store retention | Done — `prune_jobs(older_than_days)`; jobs you applied to are never pruned |
| Provider coverage | groq · gemini · openai · anthropic · qwen · openrouter · custom, all assembled by `build_chain(settings)`. Families resolve by pattern and open models by parameter size, so adding a key needs no code change |
| agentkit (Phase 1) | **Complete.** `src/agentkit/`: chat+tools IR, provider translation, measured tier registry, classified errors, circuit breaker, router (`plans_for`/`choose_strategy`), the nine strategy executors, the `ToolBox` seam, tolerant JSON, and the `Runner` that walks the plan queue. Verified live: the same task answered correctly through `native_loop` on llama-3.3-70b and through `prefetch_single_shot` on llama-3.1-8b (measured incapable of a tool loop), and a full failover walk 401→429→answer across three providers |
| Triage respected everywhere | Done — dismissed/snoozed jobs leave the digest, bot `/jobs` and `/apply` numbering; the dashboard opts out (`hide_triaged=False`) so it can still render Undo |
| Triage | Done — dismiss/snooze/note per job (triage table, POST /triage, queue count) |
| Ingest gate | Done — age/locations/drop-keywords + source selection, editable in Settings, applied before storage with per-reason drop counts |
| Dashboard v3 | Done — sidebar shell, health-first Overview, triage queue + focus mode, fit-check states, nudge banner, locked Settings (from the Claude Design project) |
| agentkit (Phase 2) | **Complete.** Permission tiers (READ/ACT/ADMIN + structural exclusion), argument-bound single-use confirmations, FTS5 knowledge index with per-chunk provenance and trust, fail-closed audit on the run_id spine, and `GuardedToolBox` — same shape as `ToolBox`, so it drops into the Runner and there is no ungoverned path |
| assistant (Phase 3) | **Complete.** `src/jobagent/assistant/`: 14 in-process tools, R2 exclusions as absences (no send/approve/ats tool exists), `CONFIG_WRITABLE` allow-list with frozen as the computed complement, impact previews dry-run over real stored rows, config snapshots + rollback, and FTS5 search over postings fenced as UNTRUSTED |
| Test suite | 463 tests, 34 test files, zero network, injectable fakes throughout |

## Known Gaps (from the July 2026 audit)

Tier 1 of the remediation roadmap is complete (API auth, PII split, browser API URL,
pipeline health, retry/backoff, docs truth-pass). Still open:

- **Dedup is weak for Telegram** — the hash is company+title+location and the
  Telegram parser never sets a company, so those postings dedup on the title line only.
- **Heuristic scores overwrite LLM scores** each run; no score provenance is kept.
- **Tag-driven role signal** lets a few postings score strongly on stack tags rather
  than the title (3 of 218 strong matches, all from one dev marketplace that tags its
  whole stack regardless of role). Left alone deliberately: tuning the scorer around
  one board's tagging habit would be overfitting.
- **FakeLLM tests cannot detect fabrication.** Two R1 violations survived a full green
  suite and were only caught by running the real model (see R1a/R1b).
- **Gemini's tool support is still unverified.** The card claims `native_tools=True`
  from the family pattern, but the free-tier quota has been exhausted on every attempt,
  so no call has ever reached it. The card's `notes` field says so; treat the claim as
  documented-not-measured until a call succeeds.
- **The Telegram handlers have no runtime test coverage.** `tests/test_bot.py` covers
  only the pure helpers in `bot/service.py`; the handlers in `bot/app.py` need live
  `Update`/`Context` objects. This is how a call to an undefined `_llm()` shipped in
  the `/apply` fit-check path and crashed it with `NameError`
  (fixed in Tier 1). `tests/test_static_checks.py` now guards that class of bug, but
  it is a static check, not real coverage — a handler harness is still missing.

## What's NOT Built Yet

- **Aggregator adapter** (JSearch/SerpApi for Indeed/LinkedIn/Glassdoor) — toggle ready, no adapter code
- **Profile/watchlist editing** in dashboard Settings (currently file-based)
- **Bot outcome-marking** (interview/offer/rejected from Telegram — currently dashboard-only)
- **Deployment to a live VPS** — systemd units + scripts are ready, needs a provisioned box

## Key Numbers

- **11,700+** jobs scored in a live run (8,253 fetched in a single pass across 6 adapters)
- **40** companies in the ATS watchlist (Greenhouse/Lever/Ashby)
- **463** tests across 34 files — all run offline, no network, no credentials
- **6** LLM providers with automatic failover (3 free, 3 paid)
