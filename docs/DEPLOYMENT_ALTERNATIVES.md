# Deployment Alternatives — Hosting Options Compared

The primary deployment is a VPS (see [DEPLOYMENT.md](DEPLOYMENT.md)). This doc
compares the alternatives you asked about and gives step-by-step setup for the ones
that actually fit, with honest verdicts. Facts verified June 2026 (free tiers
change — re-check before relying on them).

## The one thing that decides everything

Our agent has four needs, and they split the platforms cleanly:

1. **Interactive bot** (`/jobs`, `/status`) — needs an always-on process *or* webhooks.
2. **Scheduled pipeline** (ingest → match → digest) — needs cron.
3. **Persistent SQLite store** — needs a durable disk.
4. **Telegram CHANNEL scraping (Telethon)** — needs a *persistent session file* **and a
   stable IP**. Logging a Telegram **user account** in from rotating datacenter IPs
   gets it flagged/banned, and ephemeral disks lose the session. Same for **Playwright**
   auto-apply later (needs a real browser + persistent profile).

➡️ **Only a real VPS does all four for free.** Serverless / CI free tiers can do **#1–#3
for the API sources** (RemoteOK, Remotive, Greenhouse, Lever, Ashby) and **push the
digest** (Bot API is just outbound HTTPS) — but **cannot** do **#4** (Telethon channel
scraping) or Playwright Tier-2 apply. Keep that in mind: the cheaper the host, the
fewer sources you get.

## Comparison

| Platform | Truly free? | Interactive bot | Channel scraping (Telethon) | Pipeline + digest | Storage | Verdict |
|---|---|---|---|---|---|---|
| **Oracle VPS** (primary) | ✅ forever | ✅ polling | ✅ | ✅ | local disk | 🥇 Full feature set, $0 |
| **GitHub Actions** | ✅ (2000 min/mo private; ∞ public) | ❌ | ❌ | ✅ | cache/artifact | 🥈 Best zero-infra free for digest |
| **Google Cloud Run** | ✅ generous always-free | ⚠️ webhook only | ❌ | ✅ (as a Job) | external (GCS) | 🥉 Free but most complex |
| **Railway** | ❌ $5 trial then ~$5/mo | ✅ | ⚠️ (IP may rotate) | ✅ | ✅ volume | Easiest *paid* full host |
| **Render** | ⚠️ crippled free tier | ❌ (spins down 15 min) | ❌ | ⚠️ | ❌ no free durable disk | Not a clean free fit |
| **Cloudflare Workers** | ✅ | ⚠️ webhook, full rewrite | ❌ impossible | ⚠️ rewrite | D1/KV | ❌ Wrong tool — skip |

---

## Option 1 — GitHub Actions (Free) ✅ recommended free complement

**What you get:** a daily digest of API-source jobs, pushed to Telegram. No server,
no bill. **What you lose:** Telegram channel scraping and the interactive bot.

A ready workflow is committed at [`.github/workflows/digest.yml`](../.github/workflows/digest.yml).

Steps:
1. Push this repo to GitHub (done).
2. Repo → **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `OPENROUTER_API_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. Ensure `.github/workflows/digest.yml` is on your **default branch** (scheduled
   workflows only run there).
4. Actions tab → **Job digest → Run workflow** to test immediately; then it runs
   daily at 04:00 UTC (07:00 EAT).

Caveats: cron is **UTC**; runs can be delayed at busy times; GitHub **disables
scheduled workflows after 60 days of repo inactivity** (a commit re-enables);
the `data/` cache may evict after ~7 idle days (fine for daily runs).

---

## Option 2 — Cloudflare Workers (Free) ❌ not recommended

**Verdict: wrong tool for this project — skip it.** Workers run JS/TS (Python only via
Pyodide: pure-Python + Pyodide packages, 128 MB memory, CPU-time limits, **no
filesystem, no long-running process**). That means:
- **Telethon and Playwright cannot run** (native deps, sockets, browser) — ever.
- Our entire Python codebase (sqlite3, httpx clients, adapters) would need a **full
  rewrite** in JS/TS with a webhook bot + D1 for storage.

Cron Triggers (1-min min, 5 free/account) and a webhook bot are technically possible,
but rebuilding everything to gain nothing over GitHub Actions / Cloud Run isn't worth
it. Use those instead.

---

## Option 3 — Railway (~$5/mo, NOT free) ⚠️ best paid ergonomics

Railway **removed its free tier in 2023**: you get a one-time **$5 trial credit**, then
the **Hobby plan is $5/mo** (includes $5 usage). If you'll spend ~$5/mo, it's the
*easiest* full-feature host (persistent service + volume + cron), and unlike serverless
it can run the bot and *attempt* Telethon (though redeploys may rotate the IP).

Steps:
1. railway.app → sign in with GitHub → **New Project → Deploy from GitHub repo**.
2. Railway auto-detects Python (Nixpacks). Set **Start command**: `python scripts/run_bot.py`.
3. **Variables** tab → add all your `.env` keys (OPENROUTER, TELEGRAM_*, SERPAPI…).
4. **Volumes** → add a volume mounted at `/app/data` (persists SQLite + `.session`).
5. Add a **Cron service** (separate service, same repo): schedule `0 4 * * *`,
   command `python scripts/pipeline.py --top 10`.
6. For Telethon: open a one-off shell (Railway "Run command") → `python scripts/telegram_login.py`.

---

## Option 4 — Render (free tier too limited) ⚠️

Render's **free web service spins down after 15 min of inactivity** (kills the polling
bot), the free tier has **no durable disk** (persistent disks are paid — so SQLite and
the Telethon session don't survive restarts), and **free Postgres expires after 30
days**. A free **cron job** could run an API-source pipeline that writes to an external
DB, but a clean setup needs paid (~$7/mo for a background worker + disk).

If paying: create a **Background Worker** (`python scripts/run_bot.py`) + **Disk**
mounted at `/opt/render/project/src/data`, and a **Cron Job** (`python scripts/pipeline.py`).
Otherwise prefer GitHub Actions for free, or Oracle VPS for full features.

---

## Option 5 — Google Cloud Run (Free) 🥉 free but most complex

Generous **always-free** tier (2M requests, 360k GB-s, 180k vCPU-s/mo) + **free Cloud
Scheduler**. Containers scale to zero. Fits the **pipeline** well as a **Cloud Run Job**
on a Scheduler; the bot would have to run as a **webhook** service (no polling). The
filesystem is **ephemeral**, so state (SQLite + session) must live in **Cloud Storage**.
Telethon channel scraping is still not viable (rotating IP). Genuinely $0 at our scale,
but the most setup.

Pipeline-as-a-Job (the high-value, simplest slice):
1. Install `gcloud`; `gcloud init`; pick/create a project; enable Run + Scheduler +
   Artifact Registry + Storage APIs.
2. Add a `Dockerfile` (minimal):
   ```dockerfile
   FROM python:3.12-slim
   WORKDIR /app
   COPY . .
   RUN pip install -e ".[llm]"
   ENTRYPOINT ["python", "scripts/pipeline.py", "--top", "10"]
   ```
3. Externalize the store: `gsutil mb gs://<bucket>`; set `JOBAGENT_DB_PATH` to a path
   you sync to GCS at start/end (or mount the bucket via a Cloud Run GCS volume).
4. Deploy the job:
   ```bash
   gcloud run jobs deploy jobagent-pipeline --source . --region <region> \
     --set-env-vars OPENROUTER_API_KEY=...,TELEGRAM_BOT_TOKEN=...,TELEGRAM_CHAT_ID=...
   ```
5. Schedule it:
   ```bash
   gcloud scheduler jobs create http jobagent-daily \
     --schedule "0 4 * * *" --uri <run-job-trigger-url> --http-method POST \
     --oauth-service-account-email <sa>@<project>.iam.gserviceaccount.com
   ```
(Secrets are better stored in **Secret Manager** than `--set-env-vars`.)

---

## Recommendation

- **Full features, $0:** Oracle Cloud VPS → [DEPLOYMENT.md](DEPLOYMENT.md). Only option
  that runs Telegram channel scraping + interactive bot + (later) Playwright apply.
- **Dead-simple free digest, no server:** GitHub Actions (Option 1). Great as a
  fallback or to start today while you set up the VPS.
- **Willing to pay ~$5/mo for simplicity:** Railway.
- **Skip:** Cloudflare Workers (wrong runtime), Render free tier (too limited).

You can run **both** Oracle (full) and GitHub Actions (backup digest) — they're
independent and don't conflict.
