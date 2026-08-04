# Personal Job Agent — CLAUDE.md

Read `.claude/context.md`, `.claude/rules.md`, `.claude/agent.md`, `.claude/memory.md` before starting any task.

---

## 1. Quick Orientation

Self-hosted autonomous job-hunting agent. Ingests postings from Telegram channels + job boards, matches against a structured profile, surfaces ranked results via a Telegram bot, generates tailored application assets (CV, cover letter, email), fills ATS forms with HITL approval, and tracks outcomes on a dashboard.

Three services: **FastAPI** (orchestrator, port 8077) + **Telegram bot** (primary UI) + **Astro dashboard** (analytics + config).

---

## 2. Non-Negotiables

- **R2 — HITL gate**: Never submit an application without explicit user approval. Both email and ATS paths stop and show what will be sent.
- **R1 — No CV fabrication**: Tailoring reframes real experience only.
- **R3 — No CAPTCHA solving**: Hand the user a deep link on any anti-bot block.
- **R9 — Secrets in `.env` / encrypted store only**: Never commit credentials.
- **R10 — Pydantic `ConfigDict` only**: Class-based `class Config:` is banned.
- **R12 — No commit without user approval**: Surface the draft message and wait.
- **R13 — No `Co-Authored-By` trailer**: Omit the line entirely.

Full rules: `.claude/rules.md`

---

## 3. Key Commands

```bash
make install        # backend venv + dashboard deps (idempotent)
make check          # preflight: env vars, free ports, store presence
make run            # API (:8077) + dashboard (:4321), one Ctrl-C stops both
make run_bot        # Telegram bot (separate long-lived process)
make pipeline       # one ingest → match pass, no Telegram push
make test           # 234 offline tests, no credentials needed

# Single test file
.venv/bin/python -m pytest tests/test_api.py -v
```
Writes need `DASHBOARD_PASSWORD` set — the API gates every non-GET route (R19).

---

## 4. Architecture (one sentence each)

- **FastAPI** (`src/jobagent/api/app.py`) — sole backend; `create_app()` factory for testability.
- **Service layer** (`src/jobagent/`) — ingestion, matching, fit, apply, LLM, digest. Shared by bot and API.
- **Telegram bot** (`src/jobagent/bot/`) — calls service layer in-process (no HTTP hop).
- **Dashboard** (`dashboard/`) — Astro SSR, fetches FastAPI REST API.
- **Store** (`src/jobagent/store/db.py`) — SQLite, per-request open/close for thread safety.
- **MultiLLM** (`src/jobagent/llm_client.py`) — ordered failover: Groq → Gemini → OpenRouter → custom → OpenAI → Anthropic.
- **Secret store** (`src/jobagent/secrets_store.py`) — Fernet-encrypted config on disk, overlays `.env`.
- **Auth** — every non-GET API route requires a bearer token from `DASHBOARD_PASSWORD`; fails closed (R19).
- **Health** (`store.pipeline_health()`) — staleness, error count, per-source freshness; bannered in the dashboard and the digest.
- **Matching** (`matching/heuristic.py`) — preference-weighted: `skill_weights`, title-vs-body role tiers, seniority and must-have checks.
- **Lifecycle** (`core/schemas.ALLOWED_TRANSITIONS`) — enforced status graph; `correction=true` overrides and audits.
- **Run ledger** — every pipeline pass has a run_id on all its events; `GET /runs` lists summaries, `GET /runs/{id}` reconstructs a pass.
- **Eval harness** (`matching/evalset.py`) — labeled ranking floors (P@5=1.0); `scripts/eval_matching.py` is the tuning loop.
- **Ingest gate** (`ingestion/gate.py`) — rejects postings before storage on age/location/keywords; source selection via `ingest_sources` (overrides `[sources]`). Editable in Settings; drops counted per reason. Never gate on fit — see R4a.
- **Triage** (`store.set_triage` / `POST /triage/{job_id}`) — dismiss/snooze/note per job; the dashboard queue = strong ∧ untriaged; snoozes lapse on their own.

Full architecture + module map: `.claude/agent.md`

---

## 5. What NOT To Do

| Anti-pattern | Rule |
|---|---|
| Auto-submit without user approval | R2 |
| Invent CV skills/titles/dates | R1 |
| Solve CAPTCHAs | R3 |
| Share a Store across threads | R15 |
| Use `class Config:` in Pydantic models | R10 |
| Use `from __future__ import annotations` in FastAPI dep files | R11 |
| Commit without explicit user approval | R12 |
| Add `Co-Authored-By` to commits | R13 |
| Commit `.env`, `.session`, or credential files | R9 |
| Add a non-GET route without `dependencies=auth` | R19 |
| Default `JOBAGENT_CORS_ORIGINS` to `*` | R20 |
| Call `client.get` directly in an adapter (bypasses retry) | R21 |
| Put real name/email/phone in the committed `preferences.toml` | R22 |
| Add a generator that describes the candidate without passing the CV | R1a |
| Change a generator prompt without a live real-model check | R1b |
| Move an application status outside `ALLOWED_TRANSITIONS` silently | R23 |
| Add a send path for follow-up nudges | R24 |
| Scan HTML strings for CAPTCHA (use DOM selectors) | R3 |
| Hit real APIs in tests | R17 |

---

## 6. Test Patterns

- `FakeLLM` — returns canned JSON/text, no network.
- `FakePage` — injectable Playwright page for ATS tests, no browser.
- `TestClient(create_app(...))` — sync FastAPI testing with injected deps.
- `tmp_path` — isolated SQLite store per test.
- `monkeypatch` — env var overrides, settings cache reset.
- `conftest.py` — auto-resets `cfg._cached = None` around every test.

---

## 7. Configuration

- `config/preferences.toml` — target roles, skills, watchlist, source toggles (committed, placeholders for identity).
- `config/preferences.local.toml` — your name/email/phone/cv_path (gitignored, overlaid at load).
- `.env` — secrets (API keys, Telegram tokens, SMTP). See `.env.example`.
- Dashboard Settings page — auth-gated UI for LLM/Telegram/SMTP config (encrypted at rest).
- `JOBAGENT_MASTER_KEY` — Fernet key for the secret store. Generate with `python scripts/genkey.py`.
