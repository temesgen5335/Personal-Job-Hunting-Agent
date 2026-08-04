# Personal Job Agent

A self-hosted, personal job-hunting agent. It ingests job postings from Telegram
channels and job boards, scores them against **your** CV and preferences, and helps
you apply — drafting tailored CVs, cover letters, and emails, and (with your
approval) filling ATS application forms. You drive it through a **Telegram bot**; an
**Astro dashboard** is a triage cockpit: a queue of strong untriaged matches with
dismiss/snooze/note (or a one-at-a-time mode), pipeline health per source, on-demand
fit checks, application tracking with follow-up nudges, and credential editing. It runs scheduled and autonomous on a VPS, or as a free daily digest on
GitHub Actions.

**Reusable by anyone:** clone it, add your own credentials, and run your own private
instance. Nothing is hard-coded to one person — all identity lives in config.

---

## Architecture (at a glance)

```
Telegram channels ─┐
RemoteOK/Remotive  ┤
Greenhouse/Lever/  ┼─▶ ingestion adapters ─▶ SQLite store ─▶ matching (heuristic + LLM)
Ashby              ┤        (normalize+dedup)      │                     │
(aggregator: soon) ┘                               ▼                     ▼
                                          Telegram bot  ◀──────  ranked digest / /apply
                                          Astro dashboard (read-only analytics)
```
Multi-provider LLM with automatic failover (Groq → Gemini → OpenRouter → OpenAI →
Anthropic, or any OpenAI-compatible endpoint). See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

A **FastAPI orchestrator** sits between the interfaces and the data: the dashboard
calls it over REST, and the bot calls the same service layer in-process.

## Status: v3 (234 tests passing, CI on every push)
Ingestion · matching · Telegram bot (menu + filters) · Tier-1 email apply · Tier-2
ATS form-fill · multi-LLM failover · FastAPI orchestrator · Astro dashboard with
config UI, fit-checker, analytics, and pipeline health · VPS + GitHub Actions deploy.

**Security note:** every state-changing API route requires a bearer token, so
`DASHBOARD_PASSWORD` must be set for applying, status edits, fit checks, or config
changes to work. Read-only endpoints stay open. Never expose the API publicly without
it — those routes can send email as you.

---

## Setup (self-host, ~15 min)

### Prerequisites
- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- Node 18+ (only for the dashboard)
- A Telegram account + a bot from [@BotFather](https://t.me/BotFather)
- At least one LLM API key (free options below)

### 1. Install
```bash
git clone <your-fork> PersonalAgent && cd PersonalAgent
uv venv
uv pip install -e ".[telegram,llm,apply]"   # telegram reader, LLM, Playwright ATS
.venv/bin/playwright install chromium        # only if you want Tier-2 ATS form-fill
```

### 2. Configure credentials — `.env`
```bash
cp .env.example .env      # then edit
```
Fill in what you'll use:
- **LLM (pick ≥1):** `GROQ_API_KEY` / `GEMINI_API_KEY` / `OPENROUTER_API_KEY` /
  `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`, and `LLM_PROVIDER` (primary; the rest are
  automatic fallbacks). Per-provider model overrides are optional.
- **Telegram bot (talk to it):** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (your numeric id).
- **Telegram channel reading (optional):** `TELEGRAM_API_ID`/`TELEGRAM_API_HASH`
  (from [my.telegram.org](https://my.telegram.org)), `TELEGRAM_PHONE`, `TELEGRAM_CHANNELS`.
- **Email apply (optional):** `SMTP_*`, `APPLY_FROM_EMAIL`.

### 3. Configure your profile
Two files, same split as `.env.example` / `.env`:
- **`config/preferences.local.toml`** (gitignored — create it) holds your identity:
  `name`, `headline`, `cv_path`, `email`, `phone`, and `[profile.links]`. It is
  overlaid section-by-section onto the committed file, so the repo never carries
  anyone's contact details.
- **`config/preferences.toml`** (committed) holds shareable search config:
  target roles, skills, domains, must-haves, exclude-keywords, and
  **`[profile.skill_weights]`** — per-skill importance (unlisted skills weigh 1.0).
  Raise what you want to be hired for and lower generic tooling; this is what stops a
  posting that merely mentions Docker + AWS from ranking like one built on your
  differentiators.
- `[sources]` — turn whole sources on/off (`remoteok`, `greenhouse`, `telegram`, …).
- `[watchlist]` — Greenhouse/Lever/Ashby company slugs to track (add/remove freely).
- Put your CV text in `config/cv_master.md` (and PDF at the `cv_path` you set) — used
  to tailor applications. **Hard rule:** tailoring reframes real experience, never invents.

### 4. Initialize + first run
```bash
.venv/bin/python scripts/init_db.py
.venv/bin/python scripts/telegram_login.py    # one-time, only if using channel reading
.venv/bin/python scripts/pipeline.py --no-send # ingest + match (no Telegram push)
```

### 5. Use it
```bash
make run                                        # API (:8077) + dashboard (:4321) together
make run_bot                                    # the interactive bot — then DM it /menu
```
Or without make: `.venv/bin/python scripts/run_api.py`, then
`cd dashboard && npm install && npm run dev`, then `scripts/run_bot.py`. The dashboard
needs the **API running** — it is a client of it, not a direct reader of the store.

`make check` runs a preflight (missing env vars, occupied ports, absent store) and
`make install` sets up both halves. See [Running](#running) for all targets.
In Telegram: **`/menu`** → set Date/Location/keyword filters → **Show jobs** → tap **📨 N** to apply.

## LLM options (all OpenAI-compatible except Anthropic)
| Provider | Free tier | Set | Notes |
|---|---|---|---|
| Groq | ✅ generous | `GROQ_API_KEY` | fast; good default primary |
| Google Gemini | ✅ (check quota) | `GEMINI_API_KEY` | via OpenAI-compat endpoint |
| OpenRouter | ✅ `:free` models | `OPENROUTER_API_KEY` | 200+ models incl. free |
| OpenAI | ❌ paid | `OPENAI_API_KEY` | |
| Anthropic | ❌ paid | `ANTHROPIC_API_KEY` | |
| Local/OSS (Ollama, vLLM) | ✅ self-run | *(v2: custom base_url)* | any OpenAI-compatible server |

Set `LLM_PROVIDER` to your primary; the others become automatic failover backups.

## Deploy
- **Free daily digest (no server):** GitHub Actions — see [docs/DEPLOYMENT_ALTERNATIVES.md](docs/DEPLOYMENT_ALTERNATIVES.md).
- **Full autonomous (bot + scheduled ingest):** VPS — see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) (Oracle free-tier quickstart included).

## Running
```bash
make install     # backend venv + dashboard node_modules (idempotent)
make check       # preflight: .env, required vars, free ports, store presence
make run         # API (:8077) + dashboard (:4321), prefixed logs, one Ctrl-C stops both
make run_bot     # the Telegram bot (separate long-lived process)
make pipeline    # one ingest → match pass, no Telegram push
make test        # the offline test suite
```
Individual services: `make run_backend`, `make run_dashboard`. Override ports with
`make run API_PORT=9000 DASH_PORT=4322`.

**Deploying the dashboard away from the API** (e.g. dashboard on Vercel, API on a
VPS): set `PUBLIC_JOBAGENT_API_URL` to the API's public address. Browser-side actions
use it, while server-side rendering uses `JOBAGENT_API_URL`. Also add the dashboard's
origin to `JOBAGENT_CORS_ORIGINS`, which defaults to localhost only.

## Commands
```bash
.venv/bin/python scripts/pipeline.py            # ingest → match → send digest
.venv/bin/python scripts/match.py 12            # rescore + print top matches
.venv/bin/python scripts/apply.py prepare 3     # draft a Tier-1 (email) application
.venv/bin/python scripts/apply_ats.py preview 3 # Tier-2 ATS fill + screenshot (no submit)
.venv/bin/pytest -q                             # run the test suite
```

## Hard rules
See [.claude/rules.md](.claude/rules.md) (and the original [.agent/rules.md](.agent/rules.md)):
never fabricate CVs · never submit without per-job approval · prefer APIs over scraping ·
secrets only in `.env` / the encrypted store · don't fight CAPTCHA.

## Contributing / onboarding an agent
Start at [CLAUDE.md](CLAUDE.md) (or [AGENTS.md](AGENTS.md) for other tools), which
points at `.claude/`: `context.md` (problem, vision, current state), `rules.md` (hard
constraints), `agent.md` (architecture, stack, module map), `memory.md` (why the design
is the way it is). Read those before changing anything.
