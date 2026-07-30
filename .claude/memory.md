# Memory — Build History & Decisions

This file records the non-obvious decisions, constraints, and lessons learned
during development. Read this to understand *why* the code is the way it is,
not just what it does.

---

## Architecture Decisions

### Why not Hermes Agent?
The original plan used NousResearch Hermes Agent as the orchestrator (cron, memory,
LLM routing, MCP). In practice, every role Hermes would fill was covered more simply:
systemd timers for scheduling, SQLite for persistence, `MultiLLM` for provider
routing, and `preferences.toml` for profile. Hermes remains installable as an
optional agentic brain for future interactive reasoning, but the v2 system runs
without it.

### Why FastAPI + Astro, not a single full-stack framework?
The Telegram bot is the primary interface — it existed before the dashboard. FastAPI
wraps the existing Python service layer (ingestion, matching, apply) that the bot
already calls in-process. Astro was chosen for the dashboard because it generates
minimal JS, renders server-side, and doesn't require a JS framework (React/Vue/Svelte).
The dashboard is a thin read/config layer over the API, not an application in itself.

### Why SQLite, not Postgres?
Single-user self-hosted system. SQLite is zero-config, zero-process, and the entire
store is one file that backs up with `cp`. The per-request open/close pattern handles
FastAPI's threadpool. If multi-user or concurrent writes ever matter, swap to Postgres
— the Store interface is the same.

### Why multi-LLM failover?
Free-tier API keys (Groq, Gemini, OpenRouter) have aggressive rate limits and quotas.
A single provider goes down mid-digest. The `MultiLLM` chain tries each in order and
falls through on any error. Adding a paid provider just moves it to the front of the
chain; free ones remain as backups. Zero-config resilience.

### Why Fernet for the secret store?
The dashboard needs to persist LLM keys so users don't edit `.env` files on a VPS.
Fernet (symmetric, authenticated encryption) is stdlib-adjacent (`cryptography` package),
has no key-management complexity, and encrypts at rest. The master key lives in `.env`
(or environment), so the threat model is: if someone reads the encrypted file from disk,
they can't extract the secrets without the key.

---

## Process Constraints

### The two-Telegrams rule
Telegram plays two unrelated roles:
1. **Reading channels** — Telethon (MTProto, logs in as a user account). Needs `api_id`,
   `api_hash`, `phone`, and a persisted `.session` file with a stable IP.
2. **Interactive bot** — Bot API (BotFather token). Stateless HTTP, runs anywhere.

Never conflate them. The reader is in `ingestion/adapters/telegram.py`; the bot is in
`bot/`. Telethon requires a VPS with persistent storage and a stable IP (rotating IPs
get the user account flagged). Bot API works from anywhere including serverless.

### HITL gate is non-negotiable
Every submission path (email Tier 1, ATS Tier 2) shows the user exactly what will be
sent and waits for explicit approval. This is not a convenience feature — it's a safety
constraint (R2). The `approved_at` timestamp is the audit trail.

### CV tailoring vs. fabrication
The LLM prompt for `tailor_cv()` explicitly instructs: reframe and emphasize real
experience, never invent. The output traces to `base_cv_id`. This is a legal and
ethical constraint (R1), not just a preference.

---

## Known Limitations

- **No LinkedIn/Indeed/Glassdoor adapter.** These sites are aggressively anti-bot with
  no public API. The planned approach is a third-party aggregator (JSearch via RapidAPI).
  The `Sources.aggregator` toggle and `Source.aggregator` enum are ready; only the
  adapter code is missing.
- **Profile editing is file-based.** `config/preferences.toml` must be edited manually
  or via SSH. Dashboard Settings currently only covers LLM/Telegram/SMTP configuration.
- **No multi-user support.** The system is designed for one person. TELEGRAM_OWNER_ID
  gates bot access. Dashboard auth is a single shared password.
- **Playwright needs a real browser.** `playwright install chromium` must run on the
  deployment target. On ARM (Oracle Cloud), Chromium works but is heavier. ATS form-fill
  is optional — the system is fully functional without it.

---

## Version History

| Version | Commit | What changed |
|---|---|---|
| v1.0 | `80cc535` | Phase 0–4 complete: ingestion, matching, bot, Tier-1+2 apply, VPS deploy |
| v2.0 | `1a4a752` | FastAPI orchestrator; dashboard fetches API instead of direct SQLite |
| v2.1 | `7e6bf3c` | Encrypted config UI, auth-gated settings API, custom LLM provider |
| v2.2 | `be7be62` | Fit-checker (confidence score + explainable report) |
| v2.3 | `e23de67` | Application tracker + analytics (funnel, rates, timeline) |
| v2.4 | `8b86fa6` | Job detail pages, on-demand fit check, inline charts |
| v2.4+ | `7da7aae` | Portfolio theme, header fix, location filters, fit breakdown, pagination |

---

## Deployment History

- **GitHub Actions** — `.github/workflows/digest.yml` runs daily digest from API sources
  (no Telethon). Works on the free tier. Already deployed and tested.
- **VPS (systemd)** — `deploy/` has service/timer units, `scripts/install_services.sh`
  substitutes paths. Designed for Oracle Cloud Always Free ARM VM. Not yet deployed
  to a live box.
- **Dashboard** — Astro SSR, targeted for Vercel free tier or same VPS behind Caddy.
