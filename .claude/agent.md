# Agent Guide — Architecture, Tools & Frameworks

This document explains the design choices, technology stack, and module structure
so any developer or AI agent can navigate the codebase and contribute immediately.

---

## Architecture

```
┌───────────────────────────────────────────────────────────┐
│                     Oracle Cloud VM                        │
│                                                            │
│  ┌─────────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │  FastAPI :8077   │  │ Telegram Bot │  │   Telethon   │  │
│  │  (orchestrator)  │  │ (Bot API)    │  │   (reader)   │  │
│  └────────┬────────┘  └──────┬───────┘  └──────────────┘  │
│           │                  │                              │
│           ▼                  ▼                              │
│  ┌─────────────────────────────────────────────────────┐   │
│  │           Service Layer (Python modules)             │   │
│  │  ingestion · matching · fit · apply · llm · digest   │   │
│  └────────────────────┬────────────────────────────────┘   │
│                       ▼                                    │
│            ┌────────────────────┐                          │
│            │  SQLite Store      │                          │
│            │  (SSoT, on disk)   │                          │
│            └────────────────────┘                          │
│                                                            │
│  systemd timers: ingest (4h) · pipeline+digest (daily)     │
└───────────────────────────────────────────────────────────┘
         ▲                              ▲
         │ REST API                     │ SSR fetch
    ┌────┴────────┐              ┌──────┴──────┐
    │ Vercel      │              │  GitHub      │
    │ Astro SSR   │              │  Actions     │
    │ (dashboard) │              │  (digest CI) │
    └─────────────┘              └─────────────┘
```

### Data Flow

1. **Ingestion adapters** fetch from sources → normalize to `JobPosting` → dedup → store.
2. **Matching engine** scores new jobs: heuristic pre-filter (always) + optional LLM rerank.
3. **Telegram bot** pushes ranked digest; user taps to see fit-check, then apply.
4. **Apply flow** generates assets (CV variant, cover letter, email) via LLM. HITL gate.
5. **ATS executor** fills Greenhouse/Lever/Ashby forms via Playwright. Screenshot preview before submit.
6. **Dashboard** reads the same store via the FastAPI API for analytics and configuration.

### Key Design Decisions

| Decision | Rationale |
|---|---|
| SQLite, not Postgres | Single-user self-hosted system. No connection pooling, no ORM, no migrations server. Per-request open/close for thread safety. |
| FastAPI as sole backend | One process to manage. Bot calls service layer in-process (no HTTP hop = faster). Dashboard calls REST API. |
| Multi-LLM failover, free-first | `MultiLLM` chains providers: primary from `LLM_PROVIDER`, then free backups (Groq → Gemini → OpenRouter). Paid providers (OpenAI, Anthropic) only if configured. Falls through on any error. |
| Heuristic + LLM matching | Heuristic runs on every job (zero cost). LLM only reranks the top candidates. Word-boundary regex avoids substring false-positives ("Go" ≠ "going"). |
| Fernet encrypted secret store | Dashboard settings page writes to an encrypted file on disk. `.env` is the base; secret store overlays it. Config changes apply without restart via `reload_settings()`. |
| Telethon + Bot API (two Telegrams) | Telethon (MTProto, user account) for reading channels. Bot API (BotFather token) for the interactive bot. Never conflate them. |
| HITL gate for all submissions | R2. Both email (Tier 1) and ATS (Tier 2) stop and show the user what will be sent. `approved_at` stamped only on explicit action. |

---

## Technology Stack

### Backend (Python 3.11+)
| Tool | Purpose |
|---|---|
| **pydantic v2** | Domain schemas, settings, validation. `ConfigDict` only (no class Config). |
| **pydantic-settings** | `.env` loading with alias mapping. |
| **FastAPI** | REST API orchestrator. `create_app()` factory for testability. |
| **httpx** | HTTP client for ingestion adapters (async-capable, sync used). |
| **Telethon** | MTProto Telegram client for reading job-posting channels. |
| **python-telegram-bot** | Bot API wrapper for the interactive bot. |
| **openai SDK** | OpenAI-compatible backend for Groq/OpenRouter/OpenAI/Gemini. |
| **anthropic SDK** | Anthropic-specific backend (Messages API). |
| **Playwright** | Browser automation for ATS form-fill (Tier 2). Lazy import. |
| **cryptography (Fernet)** | Encrypted secret store for dashboard config. |
| **SQLite** | Single-file relational store. stdlib `sqlite3`, no ORM. |

### Dashboard
| Tool | Purpose |
|---|---|
| **Astro 5** | SSR framework. Renders server-side, fetches the FastAPI API. |
| **@astrojs/node** | Node.js SSR adapter for self-hosting. |
| No JS framework | Pure `.astro` components. No React/Vue/Svelte. Minimal JS for interactive bits (fetch on button click). |

### Infrastructure
| Tool | Purpose |
|---|---|
| **systemd** | Process management: bot service (always-on), ingest timer (4h), pipeline timer (daily). |
| **GitHub Actions** | CI test runner + daily API-source digest (no Telethon needed). |
| **uv** | Fast Python package manager (used in vps_setup.sh). |

### Testing
| Tool | Purpose |
|---|---|
| **pytest** | Test runner. `asyncio_mode=auto`. |
| **FakeLLM / FakePage / FakeSMTP** | Injectable test doubles. No network, no credentials, no browser. |
| **TestClient (FastAPI)** | Sync HTTP testing of the API without uvicorn. |
| **tmp_path + monkeypatch** | Isolated temp stores and env overrides per test. |

---

## Module Map

```
src/jobagent/
├── core/schemas.py          # JobPosting, Match, Application, CVVariant, Event, Source, enums
├── config.py                # Settings (pydantic-settings), get_settings(), reload_settings()
├── preferences.py           # Profile, Watchlist, Sources — from config/preferences.toml
├── store/db.py              # SQLite Store: upsert, query, analytics, application lifecycle
├── llm_client.py            # MultiLLM: ordered failover chain, OpenAI-compat + Anthropic backends
├── secrets_store.py         # Fernet-encrypted config store, masked_view()
├── ingestion/
│   ├── base.py              # BaseAdapter ABC (source, fetch, enabled)
│   ├── runner.py            # run_ingestion() — resilient per-adapter with RunReport
│   ├── registry.py          # build_adapters() — watchlist + env slugs, filtered by Sources
│   └── adapters/            # remoteok, remotive, greenhouse, lever, ashby, telegram
├── matching/
│   ├── heuristic.py         # heuristic_score() — word-boundary keyword matching
│   ├── llm.py               # llm_score() — LLM rerank of top candidates
│   └── engine.py            # run_matching() — heuristic always, LLM optional
├── fit.py                   # FitReport, heuristic_fit(), llm_fit(), assess_fit() — explainable
├── apply/
│   ├── flow.py              # prepare_application() + approve_and_send() (HITL gate)
│   ├── generators.py        # tailor_cv(), write_cover_letter(), draft_email()
│   ├── email_send.py        # SMTP sender with attachment
│   ├── ats/fields.py        # detect_platform(), field_plan(), CAPTCHA_SELECTORS
│   ├── ats/executor.py      # execute() (injectable page), apply_to_job() (Playwright)
│   └── ats_flow.py          # create_ats_application(), run_ats()
├── bot/
│   ├── service.py           # Pure helpers: MatchFilter, ranked_matches, jobs_text, HELP_TEXT
│   ├── app.py               # Telegram bot: /menu, /jobs, /apply, /status, callbacks
│   └── notify.py            # send_message() with chunk_text() (4096-char limit)
├── digest.py                # diversify() (per-company cap), format_matches()
├── api/app.py               # FastAPI create_app() factory — all REST endpoints
└── mcp_servers/             # (placeholder for future MCP tool servers)

dashboard/src/
├── lib/api.ts               # API client, types, fetch functions
├── layouts/Layout.astro     # Shared layout (portfolio dark theme, .topbar not .bar)
└── pages/
    ├── index.astro          # Overview: stats, funnel, analytics, timeline, top matches
    ├── jobs.astro           # Filterable job list with pagination
    ├── jobs/[id].astro      # Job detail with fit-check button
    ├── applications.astro   # Application tracker with inline status editing
    └── settings.astro       # Auth-gated config editor (LLM/Telegram/SMTP)

scripts/                     # CLI entrypoints: run_bot, run_api, pipeline, apply, etc.
deploy/                      # systemd service/timer units
config/preferences.toml      # User profile, watchlist, source toggles
```

---

## Adding a New Ingestion Adapter

1. Create `src/jobagent/ingestion/adapters/myboard.py`.
2. Subclass `BaseAdapter`. Set `.source` to a new `Source` enum value.
3. Implement `fetch(settings) -> list[JobPosting]`. Normalize all fields. Store the
   raw payload in `JobPosting.raw`.
4. Add the `Source` enum value to `core/schemas.py`.
5. Register in `ingestion/registry.py` `build_adapters()`.
6. Add a toggle to `preferences.py` `Sources` model.
7. Write tests in `tests/test_ingestion.py` (mock HTTP, verify normalization + dedup hash).

## Adding a New LLM Provider

1. If OpenAI-compatible: add an entry to `_PROVIDERS` in `llm_client.py` with the
   `base_url`. Add `{provider}_api_key` and `{provider}_model` fields to `Settings`.
2. If custom protocol: subclass alongside `AnthropicBackend` in `llm_client.py`.
3. Add to `_DEFAULT_ORDER` in `llm_client.py`.
4. Add to `MANAGED_FIELDS` in `secrets_store.py` so the dashboard can configure it.

---

## Common Pitfalls (from build history)

| Pitfall | What happened | Fix |
|---|---|---|
| Substring matching | "Go" matched "going", "RAG" matched "fragment" | Word-boundary regex in `_hits()` |
| Single-company flood | 8 Replit jobs filled top-10 | `diversify()` per-company cap |
| Blank env vars on CI | `TELEGRAM_CHAT_ID=""` → int parse crash | `field_validator` coercing blank to None |
| CSS class collision | `.bar` used for header AND chart bars → header collapsed to 8px | Renamed header to `.topbar` |
| SQLite cross-thread | Bot shared Store across event-loop and workers → crash | Per-thread Store open/close |
| CAPTCHA false positive | Scanning HTML string for "turnstile" matched shipped JS | Check rendered DOM elements via selectors |
| Settings overlay int | UI stores numbers as strings; `model_copy` skips validation | Explicit int coercion before overlay |
| `__future__ annotations` | Dep injection saw string "Request" not the class → 422 | Removed future-import from affected file |
