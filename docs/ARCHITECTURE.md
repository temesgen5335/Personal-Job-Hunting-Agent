# Architecture

## Principle
Hermes Agent is the **brain** (scheduler, persistent memory, LLM routing, MCP).
This repo provides the **tools and domain logic** it drives. We don't write an agent
loop — we write ingestion adapters, MCP tool servers, a store, and a Telegram bot,
and let Hermes' cron + MCP wiring orchestrate them.

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
1. Hermes cron fires ingestion adapters → normalized into `JobPosting` → deduped → store.
2. Matching scores new jobs vs. your profile: a word-boundary **heuristic prefilter**
   (always, no API) then an optional **LLM rerank** of the top candidates.
3. High matches pushed to the Telegram bot as cards (score + rationale + gaps).
4. You tap an action → `cv_tailor` / `cover_letter` / `email_draft` produce assets.
5. **HITL gate**: you approve → `apply_executor` sends email (Tier 1) or fills the ATS
   form and pauses for final approval before submit (Tier 2). `approved_at` stamped.
6. Everything logged to `events`; `applications` tracks lifecycle.
7. Phase 5 dashboard reads the same store — no rework.

## Tech stack
Python 3.11+ · pydantic v2 · SQLite (→ Postgres) · Telethon + python-telegram-bot ·
FastMCP · Playwright (Tier 2) · multi-provider LLM with failover
(Groq/OpenRouter/Gemini/OpenAI/Anthropic) · Hermes Agent on a VPS.
