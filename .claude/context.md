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
| Queue parity | Done (Aug 2026) — the number the badge shows and the rows `/jobs` renders are now the same set. `/jobs` passes `max_per_company=None` (the bot keeps its cap) and the page defaults to `within=any`. Verified live: 231 = 231 |
| Manual ingestion trigger | Done (Aug 2026) — "Pull Jobs" on the Overview calls `POST /ingest`, then polls `/runs/{id}` for per-source progress and reloads. Until this, nothing in any UI could start a pass, and no scheduler is live (see below). Verified live: 8,363 fetched across all 6 adapters, zero errors |
| Shared sign-in prompt | Done (Aug 2026) — `window.JA.signIn()` in `Layout.astro`: any page's 401 raises a password modal and retries once. Writes the same `jobagent_token` the Settings page does, so one session covers both. 403 (no `DASHBOARD_PASSWORD` on the API) is reported as its own case, never as a wrong password |
| Ingest gate | Done — age/locations/drop-keywords + source selection, editable in Settings, applied before storage with per-reason drop counts |
| Dashboard v3 | Done — sidebar shell, health-first Overview, triage queue + focus mode, fit-check states, nudge banner, locked Settings (from the Claude Design project) |
| agentkit (Phase 2) | **Complete.** Permission tiers (READ/ACT/ADMIN + structural exclusion), argument-bound single-use confirmations, FTS5 knowledge index with per-chunk provenance and trust, fail-closed audit on the run_id spine, and `GuardedToolBox` — same shape as `ToolBox`, so it drops into the Runner and there is no ungoverned path |
| assistant (Phase 3) | **Complete.** `src/jobagent/assistant/`: 14 in-process tools, R2 exclusions as absences (no send/approve/ats tool exists), `CONFIG_WRITABLE` allow-list with frozen as the computed complement, impact previews dry-run over real stored rows, config snapshots + rollback, and FTS5 search over postings fenced as UNTRUSTED |
| assistant interfaces (Phase 4) | **Complete + extended.** Four surfaces on one mechanism: `scripts/ask.py` (CLI), the `/assistant` dashboard page, a floating chat **bubble on every page** (`components/AssistantBubble.astro`), and Telegram `/ask`. The bubble and the page share one client (`lib/assistant.ts`) and one `localStorage` session, so a conversation continues seamlessly between them until cleared with New chat. Confirmations differ only in renderer — the CLI binds to `sha256(args)`, HTTP and Telegram send only a nonce and keep the arguments server-side. Config writes are refused on chat by construction (`Surface.CHAT` is outside `admin_surfaces`) |
| assistant hardening (Phase 5) | **Complete.** 10-case eval set scoring tool *selection*, answer *grounding* and *in-bounds* separately; `scripts/eval_assistant.py` with floors; `scripts/llm_doctor.py` explaining the chain, every model card's provenance, and per-task routing offline. Degraded-path conformance run measured **100% / 100% / 100%** |
| Profile & preferences | **Editable through the UI.** Identity, background, CV, search preferences, source toggles and the ATS watchlist all persist to a gitignored `data/profile.json` + `data/cv_master.md` overlay (three-layer merge: committed placeholders → legacy `preferences.local.toml` → writable overlay). `/profile` GET+PUT (both auth-gated — PII). Nothing personal is hardcoded; the tree carries placeholders only (R22) |
| Settings UI | Tabbed: Profile · CV & background · Search & matching · Sources & watchlist · Ingestion · LLM · Telegram · Email. Each tab saves independently against the backend that owns it (`/profile` or `/config`) |
| Test suite | 556 tests, 37 test files, zero network, injectable fakes throughout |

## Assistant cost characteristics (measured Aug 2026)

One `native_loop` turn sends ~1,258 tokens before any tool result — **1,047 of them the
14 tool schemas, resent on every turn**. A 5-step answer therefore costs ~8k tokens,
against ~350 for the same question on `prefetch_single_shot`. Not a defect, but it is
why free-tier daily budgets drain quickly, and it is the number to attack first if cost
becomes a concern (trim the tool set per turn — `GuardedToolBox.allowed` already exists
for exactly this).

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
- **Filtered job cleanup from the dashboard** — deletion is age-only and CLI-only today
  (`scripts/prune_store.py`). Designed but not built; see the TODO section below
- **No ingestion is actually scheduled anywhere.** Both triggers exist and neither is
  live: the systemd timers (`deploy/jobagent-ingest.timer`, every 4h; `.pipeline.timer`,
  daily 07:00) need the VPS above, and `.github/workflows/digest.yml` (`0 4 * * *`) needs
  repo secrets set. The evidence is in the store — ingest events cluster on four
  scattered days (2026-06-17, 07-28, 07-30, 08-07), the signature of hand-run passes, not
  a schedule. Until one is live, **"Pull Jobs" and `make pipeline` are the only
  things that refresh the store.**

---

## TODO — Filtered job cleanup on the Jobs page (planned, NOT built)

**Status: designed, not implemented.** Written 2026-08-17. Nothing below exists yet.

### What and why

"Pull Jobs" made the store grow on demand; nothing makes it shrink on demand. The store
is 14,296 jobs / 238 MB, of which 255 are strong matches — the rest is scrape residue
that costs disk, matching time, and every `/jobs` query. Today the only way to remove
anything is `scripts/prune_store.py --older-than N --apply`, which is **age-only and
CLI-only**. The operator should be able to select rows on the Jobs page and delete them.

### What already exists (build on it, do not duplicate)

| Piece | Where | Note |
|---|---|---|
| `prune_jobs(older_than_days, vacuum)` | `store/db.py:93` | Age-only. Deletes `matches`/`triage` first (FK order), spares anything in `applications`/`cv_variants`, ages on `last_seen_at` not `posted_at` |
| Dry-run-by-default CLI | `scripts/prune_store.py` | Reports before it deletes — keep that default when generalising |
| Row selection with filters | `store.get_matches()` + `MatchFilter` | Already does days / location / keywords / sources / score / triage |
| Preview-before-write pattern | `assistant/tools.py` impact previews | Dry-runs over real stored rows |

### The filters

Requested: **all**, **match threshold**, **date**, **source**. Also needed, because the
useless rows cluster there:

- **Score band** — `< threshold` (the bulk delete: 14,041 of 14,296 score under 0.7).
  The direction is the trap: "threshold 0.7" reads both ways and guessing wrong deletes
  the half you wanted to keep. The UI must say *"delete the 14,041 jobs scoring under
  70%"*, never *"threshold: 0.7"*.
- **Unscored** — jobs with no `matches` row at all (ingested, never matched).
- **Triage state** — dismissed / snoozed / untriaged. "Delete everything I dismissed" is
  the most obviously safe bulk action there is.
- **Source** — drop one board wholesale when it turns out to be noise.
- **Company / keyword** — kill a spammy employer.
- **Last seen** — `last_seen_at` older than N days, the basis `prune_jobs` already uses:
  a posting still in today's feed is live however old its `posted_at`.
- **Everything matching the current filters** — the "all" case, meaning *all rows the
  page is currently showing*, not all rows in the store.

### The one architectural rule this must follow

**Selection must run through the same filter path the list renders.** Build the delete
on `MatchFilter` / `get_matches()`, not a second hand-written WHERE clause. The queue
bug fixed on 2026-08-17 is exactly what happens otherwise: the page said 231 and the
query behind it meant 46, because two code paths answered the same question differently.
There the cost was a confusing number. Here the cost is deleting the wrong rows.

### Consequences that are easy to miss

1. **The FTS index does not notice deletions.** `agent_knowledge` is a persistent table
   in the same SQLite file, refreshed only by `reindex_postings()`, which is a *full
   rebuild* (`agentkit/knowledge.py:126`). Delete jobs without reindexing and the
   assistant keeps retrieving and citing postings that no longer exist — a confident
   answer about a deleted row, the worst failure shape this project has. **Any purge
   must reindex, or explicitly mark the index stale.**
2. **FK order is load-bearing.** `PRAGMA foreign_keys = ON`, so `matches` and `triage`
   go first. `prune_jobs` already gets this right — copy it, do not re-derive it.
3. **Never delete what you acted on.** Jobs referenced by `applications` or
   `cv_variants` are your history, not scrape data, and deleting them orphans the
   application record. This protection is unconditional and must not become a checkbox.
4. **Deleted jobs come straight back.** The next pull re-ingests anything still live on
   the board, so a purge is not a filter — the ingest gate (R4a) is where "never store
   this again" belongs. Say so in the UI or the feature reads as broken.
5. **Space is not reclaimed without `VACUUM`**, which rewrites the whole 238 MB file.
   Offer it as a separate, explicit follow-up action, not a silent part of every delete.
6. **`stats()` counts and the nav badge move under the user.** Reload or refetch after a
   purge, the same way "Pull Jobs" reloads after a run.

### Shape of the work

- **Store** — generalise to `purge_jobs(flt: MatchFilter, *, dry_run: bool = True)`
  returning `{jobs, matches, triage, kept_acted_on, sample}`. Keep `prune_jobs` as a
  thin age-only caller so `scripts/prune_store.py` and the Actions workflow are untouched.
- **API** — `POST /jobs/purge`, `dependencies=auth` (**R19**), `dry_run` defaulting to
  **true** so an omitted field can never delete. Log a `purge` event on the run-id spine
  so the ledger records what went and why.
- **Dashboard** — a "Clean up" control on `/jobs` that reuses the filter form already
  there. Two-step: preview (counts by table, a sample of titles, and the spared count)
  → explicit confirm. Never a one-click delete on a filter the user has not seen resolve.

### Decisions to settle before coding

- Does a **triage note** protect a row? A note is the operator's own work, like an
  application — argues for sparing. Counter-argument: you often note *why* something is
  junk. Lean toward sparing, and say which it does in the preview.
- Preview sample size: enough to be convincing, and **query wider than you show** so the
  count is never confused with the cap (**R32**).
- Hard cap per purge, or unbounded? A 14k-row delete in one request is a long-held write
  lock on a store the dashboard is reading.

### Explicitly out of scope

**No assistant tool for this.** Per `agent.md` — *"before adding an ADMIN tool, ask
whether the action moves data"* — a `delete_jobs` tool destroys data, so it belongs in
the frozen complement. **Do not register it and then gate it** (R26): exclusion has no
code path to attack, a gate has one. A prompt-injected "clean up my old jobs" must have
no tool to reach for. The assistant may keep *reporting* what a purge would remove.

### Tests to write with it

- Preview and apply select the identical row set (the parity property above).
- A job with an application survives every filter combination, including "all".
- FK order: no `FOREIGN KEY constraint failed` on a job carrying matches and triage.
- `dry_run` defaults to true — a request with the field omitted deletes nothing.
- After a purge, an assistant search cannot return a deleted posting.
- Route-table test still passes: the new route carries `dependencies=auth`.

## Key Numbers

- **11,700+** jobs scored in a live run (8,253 fetched in a single pass across 6 adapters)
- **40** companies in the ATS watchlist (Greenhouse/Lever/Ashby)
- **556** tests across 37 files — all run offline, no network, no credentials
- **6** LLM providers with automatic failover (3 free, 3 paid)
