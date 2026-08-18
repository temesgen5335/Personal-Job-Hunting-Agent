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
instance. No identity, search profile, or company watchlist is committed — the repo
ships templates, and everything personal lives in gitignored config you own.

**It runs with zero credentials.** Five of the six sources are public APIs and matching
falls back to heuristics with no LLM key, so `make install && make pipeline` gives you
real ranked jobs before you sign up for anything.

---

## Architecture (at a glance)

```
Telegram channels ─┐
RemoteOK/Remotive  ┤
Greenhouse/Lever/  ┼─▶ ingestion adapters ─▶ SQLite store ─▶ matching (heuristic + LLM)
Ashby              ┤        (normalize+dedup)      │                     │
(aggregator: soon) ┘                               ▼                     ▼
                                          Telegram bot  ◀──────  ranked digest / /apply
                                          Astro dashboard (analytics + triage)
                                          Assistant     ◀──────  ask it about any of this
```
Multi-provider LLM with automatic failover (Groq → Gemini → OpenRouter → OpenAI →
Anthropic, or any OpenAI-compatible endpoint). See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

A **FastAPI orchestrator** sits between the interfaces and the data: the dashboard
calls it over REST, and the bot calls the same service layer in-process.

## Status: v3.4 (617 tests passing, CI on every push)
Ingestion · matching · Telegram bot (menu + filters) · Tier-1 email apply · Tier-2
ATS form-fill · multi-LLM failover · FastAPI orchestrator · Astro dashboard with
config UI, fit-checker, analytics, and pipeline health · VPS + GitHub Actions deploy
· **an assistant that can answer questions about the whole system**.

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

### 0. The fast path

```bash
git clone https://github.com/temesgen5335/personalAgent && cd personalAgent
make install       # venv + dashboard deps
make setup         # interactive: .env + your profile (safe to re-run)
make pipeline      # ingest + match — no credentials needed
make run           # API :8077 + dashboard :1234
```

Just want to look at it first? `make demo` seeds a throwaway store with fictional
postings so every page has something to show, without touching your real one:

```bash
make demo
JOBAGENT_DB_PATH=data/demo.db make run
```

Prefer containers? `make docker_up` (see [step 6](#6-docker)).

The rest of this section is what `make setup` does, for anyone who would rather do it
by hand.

### 1. Install
```bash
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

**This step is not optional.** Matching scores every posting against your roles, skills
and weights, so an unedited profile gives generic results — `make check` will say so
while it is still the template.

Two ways, same destination:

**A. In the browser (recommended).** Start the app (step 5) and open
**Settings → Profile**. Every tab — identity, CV, search preferences, sources,
watchlist — saves to a gitignored `data/profile.json` overlay. Nothing personal ever
touches the repo.

**B. In a file.**
```bash
cp config/preferences.example.toml config/preferences.toml   # gitignored
```
- `[profile]` — identity plus what defines the search: `target_roles`, `core_skills`,
  `domains`, `must_haves`, `exclude_keywords`, `preferred_locations`.
- **`[profile.skill_weights]`** — per-skill importance (unlisted skills weigh 1.0).
  Raise what you want to be hired for and lower generic tooling; this is what stops a
  posting that merely mentions Docker + AWS from ranking like one built on your
  differentiators.
- `[sources]` — turn whole sources on/off (`remoteok`, `greenhouse`, `telegram`, …).
- `[watchlist]` — Greenhouse/Lever/Ashby company slugs to track. **Replace the examples**
  — these are the employers polled directly.

Your CV text goes in **Settings → CV & background** (stored at `data/cv_master.md`), and
the PDF at whatever `cv_path` you set — that PDF is what gets attached to email
applications. **Hard rule:** tailoring reframes real experience, never invents.

Layering, lowest priority first: `preferences.example.toml` (fallback for a fresh clone)
→ `preferences.toml` → `preferences.local.toml` (legacy) → `data/profile.json` (what the
UI writes, and the only layer ever written).

### 4. Initialize + first run
```bash
.venv/bin/python scripts/init_db.py
.venv/bin/python scripts/telegram_login.py    # one-time, only if using channel reading
.venv/bin/python scripts/pipeline.py --no-send # ingest + match (no Telegram push)
```

### 5. Use it
```bash
make run                                        # API (:8077) + dashboard (:1234) together
make run_bot                                    # the interactive bot — then DM it /menu
```
Or without make: `.venv/bin/python scripts/run_api.py`, then
`cd dashboard && npm install && npm run dev`, then `scripts/run_bot.py`. The dashboard
needs the **API running** — it is a client of it, not a direct reader of the store.

`make check` runs a preflight (missing env vars, occupied ports, absent store) and
`make install` sets up both halves. See [Running](#running) for all targets.
In Telegram: **`/menu`** → set Date/Location/keyword filters → **Show jobs** → tap **📨 N** to apply.

### Ask it things

```bash
make ask Q="is the pipeline healthy?"
make ask Q="which strong matches am I ignoring?"
make doctor                                     # why is it using that model? (offline)
```
Also at `/assistant` in the dashboard, and `/ask <question>` in Telegram.

The assistant reads your pipeline, runs, queue, applications and settings. **It cannot
send, submit or approve anything** — no such tool exists, which is a structural
property rather than a rule it follows. When something needs sending it hands you a
link instead. Config changes are limited to an explicit allow-list (search filters and
which model answers); anything that decides *where data goes* or *who can reach the
system* is frozen and cannot be delegated.

It degrades rather than failing: on a model too weak to run a tool loop, the retrieval
runs in Python and the model only writes the answer. Measured on the free tier at
100% tool-selection and 100% answer-grounding through that degraded path.

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

### 6. Docker

```bash
cp .env.example .env      # or: make setup
make docker_up            # API + dashboard; data/ is a mounted volume
make docker_down
docker compose --profile bot up -d          # add the Telegram bot
docker compose run --rm pipeline            # one ingest+match pass
```

Host ports bind to `127.0.0.1` only, because GET routes are unauthenticated — see
[SECURITY.md](SECURITY.md) before changing that. Playwright is not installed by
default (it adds ~400 MB); build with `--build-arg WITH_BROWSER=1` if you want
Tier-2 ATS form-fill.

## Deploy
- **Free daily digest (no server):** GitHub Actions — see [docs/DEPLOYMENT_ALTERNATIVES.md](docs/DEPLOYMENT_ALTERNATIVES.md).
- **Full autonomous (bot + scheduled ingest):** VPS — see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) (Oracle free-tier quickstart included).

## Running
```bash
make install     # backend venv + dashboard node_modules (idempotent)
make check       # preflight: .env, required vars, free ports, store presence
make run         # API (:8077) + dashboard (:1234), prefixed logs, one Ctrl-C stops both
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

## Versions & roadmap
Current release and what changed: [CHANGELOG.md](CHANGELOG.md).
What ships next and why, in priority order: [docs/ROADMAP.md](docs/ROADMAP.md).
How versions are decided (SemVer, scoped to *your data and config* rather than a Python
API): [docs/VERSIONING.md](docs/VERSIONING.md).

## Hard rules
See [.claude/rules.md](.claude/rules.md) (and the original [.agent/rules.md](.agent/rules.md)):
never fabricate CVs · never submit without per-job approval · prefer APIs over scraping ·
secrets only in `.env` / the encrypted store · don't fight CAPTCHA.

## Contributing / onboarding an agent
Start at [CLAUDE.md](CLAUDE.md) (or [AGENTS.md](AGENTS.md) for other tools), which
points at `.claude/`: `context.md` (problem, vision, current state), `rules.md` (hard
constraints), `agent.md` (architecture, stack, module map), `memory.md` (why the design
is the way it is). Read those before changing anything.
