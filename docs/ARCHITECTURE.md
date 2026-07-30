# Architecture

## Principle
There is no agent framework in this system, and no LLM-driven control loop. The
pipeline is deterministic plumbing — ingest → dedup → score → surface → assist — and
the LLM is called only where judgment actually helps: reranking matches, assessing
fit, and drafting application text. Scheduling is **systemd timers** (or GitHub
Actions); the **FastAPI orchestrator** is the single backend; **SQLite** is the store.

> **Historical note.** Early plans (and `docs/PLAN.md`) framed *Hermes Agent* as the
> orchestrating "brain" with MCP tool servers. That was never implemented: every role
> it would have filled is covered more simply by systemd timers, `MultiLLM` provider
> failover, and `preferences.toml`. Ignore Hermes/MCP references in older docs — the
> code is the authority.

## The two-Telegrams rule (most important design point)
"Telegram" plays two unrelated roles and needs two different mechanisms:

| Role | Mechanism | Credential |
|---|---|---|
| **Read job-posting channels** | Telethon (MTProto, logs in as you) | `api_id` / `api_hash` from my.telegram.org |
| **Talk to your agent** | Bot API | BotFather token |

Never conflate them. The reader lives in `ingestion/adapters/telegram.py`; the bot in `bot/`.

## Source tiers (drives the ingestion design)
| Tier | Sources | Strategy | Risk |
|---|---|---|---|
| Clean API | RemoteOK, Remotive, Greenhouse, Lever, Ashby | direct API client | none |
| MTProto | Telegram channels | Telethon (your account) | low (rate hygiene) |
| **No API, hostile** | Indeed, LinkedIn, Glassdoor, JobRight | **aggregator** (SerpApi Google Jobs primary, Apify secondary) | high — never scrape directly first |
| Last resort | any board w/o feed | Playwright | fragile |

Indeed/LinkedIn/Glassdoor/JobRight have no usable public API and are aggressively
anti-bot. We treat them as a single **aggregator adapter**, not four scrapers.

## Component diagram
*(The `HERMES AGENT` box below is historical — see the note above. Read it as
"systemd timers + the FastAPI orchestrator".)*
```
                          ┌─────────────────────────────────────────┐
                          │              VPS (systemd)               │
   Telegram channels ──┐  │   ┌──────────────────────────────────┐   │
   (Telethon)          ├──┼──▶│        INGESTION ADAPTERS        │   │
   RemoteOK/Remotive   ┤  │   │  → normalize → dedup → store     │   │
   GH/Lever/Ashby      ┤  │   └───────────────┬──────────────────┘   │
   Aggregator(SerpApi) ┤  │                   ▼                      │
   Playwright fallback ┘  │   ┌──────────────────────────────────┐   │
                          │   │   STORE (SQLite → Postgres)      │   │
   ┌────────────────┐     │   │  jobs·matches·applications·      │   │
   │  HERMES AGENT  │◀────┼──▶│  cv_variants·events  (SSoT)      │   │
   │  cron·memory·  │     │   └───────────────┬──────────────────┘   │
   │  LLM·skills    │     │                   ▼                      │
   └───────┬────────┘     │   ┌──────────────────────────────────┐   │
           │              │   │  MCP TOOLS: match_score·cv_tailor│   │
           ▼              │   │  cover_letter·email_draft·       │   │
   ┌────────────────┐     │   │  apply_executor·tracker          │   │
   │  TELEGRAM BOT  │     │   └──────────────────────────────────┘   │
   │   (Bot API)    │     └──────────────────────────────────────────┘
   └────────────────┘   ◀── /jobs /preferences /approve /status
```

## Data flow
1. A systemd timer (or `make pipeline`) fires ingestion adapters → normalized into `JobPosting` → deduped → store.
2. Matching scores new jobs vs. your profile: a word-boundary **heuristic prefilter**
   (always, no API) then an optional **LLM rerank** of the top candidates.
3. High matches pushed to the Telegram bot as cards (score + rationale + gaps).
4. You tap an action → `cv_tailor` / `cover_letter` / `email_draft` produce assets.
5. **HITL gate**: you approve → the apply flow sends email (Tier 1) or fills the ATS
   form and pauses for final approval before submit (Tier 2). `approved_at` stamped.
   Over HTTP this requires an authenticated caller — see Security posture.
6. Everything logged to `events`; `applications` tracks lifecycle. `pipeline_health()`
   reads that trail to answer "is the agent still alive?".
7. The dashboard reads the same data through the FastAPI orchestrator.

## Tech stack
Python 3.11+ · pydantic v2 · **FastAPI** (orchestrator, port 8077) · SQLite
(→ Postgres) · Telethon + python-telegram-bot · Playwright (Tier 2) · multi-provider
LLM with failover (Groq/Gemini/OpenRouter/OpenAI/Anthropic + any OpenAI-compatible
endpoint) · **Astro** SSR dashboard · systemd timers on a VPS.

## Security posture
Every state-changing API route (`/apply/*`, `/ats/*`, `PATCH /applications/*`,
`/ingest`, `/match`, `/fit`, `/config`) requires a bearer token derived from
`DASHBOARD_PASSWORD`, and fails closed when no password is set. Read-only routes stay
open so the dashboard can render server-side. `JOBAGENT_CORS_ORIGINS` defaults to
localhost. The rationale: these routes can send email as you, so port reachability
must not equal authority.

## Resilience
Source HTTP goes through `get_with_retry` — bounded exponential backoff with full
jitter, retrying 429/5xx and transport errors while treating 4xx as permanent, and
honoring a capped `Retry-After`. A failing adapter logs an `error` event and the run
continues. `Store.pipeline_health()` derives staleness, a windowed error count, and
per-source freshness from the `events` trail; the dashboard banners it and the daily
digest carries a warning prefix, so a degraded run cannot look like a clean one.
