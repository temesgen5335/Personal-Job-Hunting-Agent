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
routing, and `preferences.toml` for profile.

**No Hermes code was ever written, and none remains.** The `[mcp]` extra and the empty
`mcp_servers/` placeholder were removed in Tier 1 because they advertised an
integration that did not exist. If an agentic layer is ever wanted, add it
deliberately — do not treat the old docs as a partially-built foundation.

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

## The July 2026 audit

A skeptical audit of the whole system found the core pipeline real and working, but
three systemic problems worth remembering, because they are the shape of what goes
wrong here:

1. **Two auth models, one enforced.** The bot had a real owner-gate; the API — which
   by v2 could do everything the bot could — had auth only on `/config`. Every
   mutation finding traced to this. `/apply/{id}/approve` answered **200** to an
   anonymous caller. Fixed in Tier 1 (R19), with a route-table test as the net.
2. **Documentation lagged the code by a full major version.** README claimed v1 /
   72 tests / "read-only dashboard"; `pyproject.toml` and `ARCHITECTURE.md` claimed
   Hermes Agent orchestration and MCP tool servers that had never been written. The
   lesson: aspirational docs age into lies. Fixed in Tier 1.
3. **Failures were logged but never surfaced.** The `events` table captured errors
   nothing ever read, and stdout was the only alert channel. Fixed in Tier 1 via
   `pipeline_health()` + digest banner.

A fourth, still open: **git history retains the PII and CV** that Tier 1 removed from
tracking. That needs `git filter-repo` and a force-push — the owner's call, and it
should happen before the repo is ever public.

## The R1 near-miss (Tier 2, July 2026)

Tier 2 verification against the *real* LLM found two fabrication bugs that a fully
green 172-test suite had not:

- `draft_email` — the email actually sent to employers — claimed "over 8 years of
  experience" for a candidate with 3+, and asserted three technologies absent from the
  CV. Cause: `email_prompt` passed the job ad but no CV, so the employer's requirement
  list was the only material available and the model mirrored it back.
- `draft_followup` invented "over 5 years of experience" for the same reason, plus a
  literal `[date of application, 11 days ago]` placeholder in sendable text.

Also found: models emit literal newlines inside JSON strings, so strict `json.loads`
failed and the fallback returned the **entire raw JSON blob as the email body**. An
employer would have received `{"subject": ...}`.

Two durable lessons, now rules R1a/R1b:
1. Any generator that describes the candidate must receive the CV, or be forbidden from
   making claims at all.
2. FakeLLM tests verify plumbing, never truthfulness. Prompt changes need a live run
   against a job ad the CV does not satisfy — that is the only way this class of bug
   becomes visible.

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
| gov | `e1d5a12` | `.claude/` governance dir + `CLAUDE.md`/`AGENTS.md` entrypoints |
| gov | `f7556b4` | Makefile runner (install / check / run) |
| Tier 1 | `608b483`..`7ff2ea8` | Audit remediation: API auth on writes, CORS default, browser API URL, retry/backoff, pipeline health, PII split, docs truth-pass |

---

## Deployment History

- **GitHub Actions** — `.github/workflows/digest.yml` runs daily digest from API sources
  (no Telethon). Works on the free tier. Already deployed and tested.
- **VPS (systemd)** — `deploy/` has service/timer units, `scripts/install_services.sh`
  substitutes paths. Designed for Oracle Cloud Always Free ARM VM. Not yet deployed
  to a live box.
- **Dashboard** — Astro SSR, targeted for Vercel free tier or same VPS behind Caddy.
