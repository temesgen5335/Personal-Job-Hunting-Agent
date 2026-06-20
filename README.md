# Personal Job Agent

A self-hosted, personal job-hunting agent. It ingests job postings from Telegram
channels and job boards, scores them against **your** CV and preferences, and helps
you apply — drafting tailored CVs, cover letters, and emails, and (with your
approval) filling ATS application forms. You drive it through a **Telegram bot**, and
a read-only **Astro dashboard** shows analytics. It runs scheduled and autonomous on
a VPS, or as a free daily digest on GitHub Actions.

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

## Status: v1 complete (all phases, 72 tests passing)
Ingestion · matching · Telegram bot (menu + filters) · Tier-1 email apply · Tier-2
ATS form-fill · multi-LLM failover · Astro dashboard · VPS + GitHub Actions deploy.
Next: see [docs/V2_PLAN.md](docs/V2_PLAN.md).

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

### 3. Configure your profile — `config/preferences.toml`
- `[profile]` — your name, headline, target roles, skills, domains, must-haves,
  exclude-keywords, email/phone, and links.
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
.venv/bin/python scripts/run_bot.py            # interactive bot — then DM it /menu
cd dashboard && npm install && npm run dev      # dashboard at http://localhost:4321
```
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

## Commands
```bash
.venv/bin/python scripts/pipeline.py            # ingest → match → send digest
.venv/bin/python scripts/match.py 12            # rescore + print top matches
.venv/bin/python scripts/apply.py prepare 3     # draft a Tier-1 (email) application
.venv/bin/python scripts/apply_ats.py preview 3 # Tier-2 ATS fill + screenshot (no submit)
.venv/bin/pytest -q                             # run the test suite
```

## Hard rules
See [.agent/rules.md](.agent/rules.md): never fabricate CVs · never submit without
per-job approval · prefer APIs over scraping · secrets only in `.env` · don't fight CAPTCHA.
