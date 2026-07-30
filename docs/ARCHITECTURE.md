# Architecture

## Principle
There is no agent framework in this system, and no LLM-driven control loop. The
pipeline is deterministic plumbing — ingest → dedup → score → surface → assist — and
the LLM is called only where judgment actually helps: reranking matches, assessing
fit, and drafting application text. Scheduling is **systemd timers** (or GitHub
Actions); the **FastAPI orchestrator** is the single backend; **SQLite** is the store.

> **Historical note.** Early plans (`docs/PLAN.md`) framed *Hermes Agent* as an
> orchestrating "brain" with MCP tool servers. None of that was ever implemented, and
> the placeholders were removed: every role it would have filled is covered more
> simply by systemd timers, `MultiLLM` provider failover, and `preferences.toml`.
> Ignore Hermes/MCP references in older docs — the code is the authority.

## Component diagram

```
             SOURCES                    ONE PROCESS PER BOX, ONE HOST (VPS)
  Telegram channels (Telethon) ─┐
  RemoteOK / Remotive           ├──▶ ┌─────────────────────────────────────────┐
  Greenhouse / Lever / Ashby    │    │ pipeline.py  (systemd timer / Actions)  │
  (aggregator: planned)        ─┘    │  run_id ─ ingest → match → digest ──────┼──▶ Telegram DM
                                     └──────────────────┬──────────────────────┘    (daily digest +
                                                        ▼                            health banner +
      ┌──────────────────┐  in-process  ┌─────────────────────────────────┐          follow-up nudges)
      │  TELEGRAM BOT    │─────────────▶│         SERVICE LAYER           │
      │  (Bot API,       │              │ ingestion · matching · fit ·    │
      │  owner-gated)    │              │ apply (Tier 1 email / Tier 2    │
      └──────────────────┘              │ ATS via Playwright) · llm ·     │
                                        │ digest · secrets                │
      ┌──────────────────┐    REST      └────────────────┬────────────────┘
      │  ASTRO DASHBOARD │───────────▶ ┌─────────────────┴────────────────┐
      │  (SSR, reads     │             │      FASTAPI  ORCHESTRATOR       │
      │  open; writes    │             │  :8077 — sole backend; bearer    │
      │  carry bearer    │             │  auth on every non-GET route     │
      │  token)          │             └────────────────┬─────────────────┘
      └──────────────────┘                              ▼
                                        ┌──────────────────────────────────┐
                                        │        SQLITE STORE (SSoT)       │
                                        │ jobs · matches · applications ·  │
                                        │ cv_variants · events (run ledger,│
                                        │ health, audit trail)             │
                                        └──────────────────────────────────┘
```

The bot calls the service layer **in-process** (no HTTP hop); the dashboard calls the
REST API. Both read and write the same store, and every write path funnels through
the same service functions.

## The two-Telegrams rule (most important design point)
"Telegram" plays two unrelated roles and needs two different mechanisms:

| Role | Mechanism | Credential |
|---|---|---|
| **Read job-posting channels** | Telethon (MTProto, logs in as you) | `api_id` / `api_hash` from my.telegram.org |
| **Talk to your agent** | Bot API | BotFather token |

Never conflate them. The reader lives in `ingestion/adapters/telegram.py`; the bot in
`bot/`. Telethon needs a stable IP and a persisted `.session` file (a user account
logging in from rotating IPs gets flagged); the Bot API is plain outbound HTTPS and
runs anywhere, including CI.

## Source tiers (drives the ingestion design)
| Tier | Sources | Strategy | Risk |
|---|---|---|---|
| Clean API | RemoteOK, Remotive, Greenhouse, Lever, Ashby | direct API client via `get_with_retry` | none |
| MTProto | Telegram channels | Telethon (your account) | low (rate hygiene) |
| **No API, hostile** | Indeed, LinkedIn, Glassdoor | **aggregator** (JSearch/SerpApi) — *planned, toggle exists, no adapter yet* | high — never scrape directly |
| Last resort | any board w/o feed | Playwright | fragile |

## Data flow
1. A systemd timer (or `make pipeline`) starts a pass. The pass mints a **run_id**
   that rides every event it emits.
2. Ingestion adapters fetch → normalize into `JobPosting` (full payload kept in
   `raw`) → dedup by `sha256(company|title|location)` → upsert. One bad source logs
   an `error` event and the rest continue.
3. Matching scores **every** job with the preference-weighted heuristic (no API
   cost): tiered role signal (title ≫ tags ≫ body), weighted skill coverage
   (`[profile.skill_weights]`), seniority-mismatch dampening, must-have checks,
   exclusion ceiling. An optional **LLM rerank** rescores the top ~30.
4. The digest DMs the top matches — prefixed by a **health banner** when the run was
   degraded and followed by **follow-up reminders** for applications gone quiet.
5. `/apply` (bot) or the dashboard drafts assets: tailored CV, cover letter, email —
   all grounded on `cv_master.md` (R1: reframe, never invent; the email prompt
   receives the CV precisely so the model cannot mirror the job's requirements back
   as the candidate's background).
6. **HITL gate**: nothing is sent without explicit approval. Tier 1 emails via SMTP;
   Tier 2 fills Greenhouse/Lever/Ashby forms via Playwright, screenshots for preview,
   and pauses again before submit. CAPTCHA → hand back a deep link, never solve.
7. Application statuses move through an enforced lifecycle
   (`ALLOWED_TRANSITIONS`); out-of-process corrections require an explicit, audited
   flag. Follow-ups are **drafts only** — no send path exists.
8. The dashboard reads everything through the API: analytics, funnel, pipeline
   health, the run ledger, and the same fit checker the bot uses.

## Matching quality
The scorer has a regression net beyond unit tests: a labeled eval set
(`matching/evalset.py`) covering every trap class that has actually bitten —
substring hits, seniority/management mismatches, requirement-mirroring buzzword
bodies, exclusions, tag-flooding. `tests/test_eval_matching.py` pins precision@5 =
1.0, precision/recall@10 ≥ 0.9, separation ≥ 0.42; `scripts/eval_matching.py` prints
the ranked table for tuning. Floors sit at measured reality; the one known miss
(tag-flooding) is documented in the dataset rather than hidden.

## Observability
One **run_id** threads ingest → match → digest. Matching logs a `match` event; each
pass ends with a `run` summary (duration, per-stage counts, digest outcome) that
`GET /runs` lists and `GET /runs/{id}` expands into the pass's full event sequence.
`Store.pipeline_health()` derives staleness, a windowed error count, and per-source
freshness from the same trail; the dashboard banners staleness and the digest carries
a warning prefix, so a silently dead pipeline is a state the UI cannot render as
healthy.

## Security posture
Every state-changing API route (`/apply/*`, `/ats/*`, `PATCH /applications/*`,
`/ingest`, `/match`, `/fit`, `/followups/*/draft`, `/config`) requires a bearer token
derived from `DASHBOARD_PASSWORD`, and **fails closed** when no password is set.
Read-only routes stay open so the dashboard renders server-side.
`JOBAGENT_CORS_ORIGINS` defaults to localhost. Rationale: these routes can send email
as you, so port reachability must not equal authority. A route-table test fails if a
new write route ships ungated. Secrets live in `.env` or the Fernet-encrypted store
(masked on read); identity lives in gitignored `config/preferences.local.toml`, so
the committed config carries placeholders only.

## Resilience
Source HTTP goes through `get_with_retry` — bounded exponential backoff with full
jitter, retrying 429/5xx and transport errors while treating other 4xx as permanent,
honoring a capped `Retry-After`. A failing adapter logs an `error` event and the run
continues. Recovery is graceful everywhere: LLM providers fail over in an ordered
chain, a missing LLM degrades matching to heuristic-only and fit to the offline
report, and a failed digest send is reported in the run summary rather than crashing
the pass.

## Tech stack
Python 3.11+ · pydantic v2 · **FastAPI** (orchestrator, :8077) · SQLite (→ Postgres
if ever needed) · Telethon + python-telegram-bot · Playwright (Tier 2) ·
multi-provider LLM failover (Groq/Gemini/OpenRouter/OpenAI/Anthropic + any
OpenAI-compatible endpoint) · **Astro** SSR dashboard · systemd timers on a VPS ·
GitHub Actions for the serverless digest.
