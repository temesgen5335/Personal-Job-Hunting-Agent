# v2 Plan — Configurable, reusable, fit-aware

v1 (tagged `v1.0.0`) is complete and working: ingestion → matching → Telegram bot →
Tier-1/Tier-2 apply → dashboard, multi-LLM failover, VPS + Actions deploy. v2 turns it
into a polished, self-hostable product.

## Decisions (locked)
- **Reuse model:** self-host per user. Each person runs their own instance; config is
  per-instance. No multi-tenant accounts/SaaS.
- **Backend:** introduce a **FastAPI orchestrator** as the single service. The Telegram
  bot AND the Astro dashboard both become clients of it. This is the originally-planned
  sole-mutator tier, right-sized: **Astro → FastAPI → SQLite**.
- **Bot is primary.** Dashboard is for configuration + analytics; the bot stays the
  fastest way to interact once deployed.

## Architecture shift
```
            ┌──────────────┐        ┌──────────────┐
 Telegram ──▶│              │        │ Astro        │
   bot       │   FastAPI    │◀──────▶│ dashboard    │
            │ orchestrator │  REST  │ (config+UI)  │
 schedulers ▶│ (sole writer)│        └──────────────┘
            └──────┬───────┘
                   ▼
              SQLite store + secrets store (encrypted, gitignored)
```
The existing Python modules (ingestion, matching, apply, llm_client) become the
service layer FastAPI exposes; the bot and dashboard stop touching the store directly.

## Phases

### v2.0 — FastAPI orchestrator (foundation) ✅
- [x] FastAPI app (`jobagent/api/app.py`, `create_app` factory) wrapping the service
      layer: GET /health /stats /jobs (filtered) /applications; POST /match,
      /ingest (background), /apply/prepare, /apply/{id}/approve, /ats/preview, /ats/{id}/submit
- [x] Per-request Store (SQLite thread-safety); injectable settings/llm/mailer for tests
- [x] scripts/run_api.py (uvicorn); 6 API tests via TestClient (78 total)
- [x] Live-verified on the real store (7,345 jobs, multi-LLM chain, filters)
- [x] Astro dashboard now fetches the API (better-sqlite3 removed); live-verified E2E
- [x] Bot keeps calling the shared service modules in-process (no HTTP hop — faster,
      no risk to a working bot); both bot and API exercise the same service layer
- **Exit:** dashboard runs against the API; bot + API share one service layer. ✅

### v2.1 — Config UI + secret management ✅
- [x] **Encrypted secret store** (`secrets_store.py`, Fernet via JOBAGENT_MASTER_KEY)
      holding LLM keys/provider/models, Telegram tokens, SMTP, custom endpoint
- [x] `config.get_settings()` overlays the store on env so api/bot/pipeline agree;
      `reload_settings()` applies edits live within the API process
- [x] **Custom OpenAI-compatible provider** (Ollama/vLLM/any base_url) in the failover chain
- [x] Auth-gated config API: POST /auth/login, GET/PUT /config (admin password, fail-closed)
- [x] secrets masked on read; encrypted at rest (verified); 9 tests (87 total); live-verified
- [x] Dashboard **Settings page** + login (settings.astro): edit LLM/Telegram/SMTP from
      the UI; secrets blank-to-keep; CORS enabled; live-verified end to end
- [ ] (later) surface profile/sources/watchlist editing (preferences.toml) in the UI
- Security: secrets-in-UI is acceptable for a self-hosted single-user box behind your
  own network; the auth gate + encryption-at-rest are mandatory (both implemented).
- **Exit:** a fresh user configures everything from the dashboard, no file editing.

### v2.2 — Fit-checker (confidence score + explainable report) ✅
- [x] `fit.py`: heuristic_fit (ATS keyword coverage, no API) + llm_fit (matched/missing
      requirements, experience read, confidence) + assess_fit (LLM with heuristic fallback)
- [x] API `POST /fit {job_id}` → FitReport
- [x] Bot `/apply` shows the fit (confidence % + matched + gaps) before drafting/filling
- [x] 7 tests (94 total); live-verified (Groq: 80% fit with JD-specific gaps)
- [ ] (later) surface fit in the dashboard job/detail view
- **Exit:** /apply shows a fit score + breakdown. ✅

### v2.3 — Application tracker + analytics
- Track applied jobs (not just pulled): status pipeline, response/interview/offer.
- Dashboard analytics: applied funnel, response rate, fit-vs-outcome, source
  effectiveness, activity over time.
- **Exit:** overview shows the full funnel from pulled → matched → applied → outcome.

### v2.4 — UI polish
- Clean, interactive dashboard: charts, filters, job/application detail views.
- Keep it simple and fast; no heavy framework creep.
- **Exit:** a dashboard that's pleasant to live in daily.

## Cross-cutting
- Hard rules from v1 carry forward (no CV fabrication, no submit without approval,
  APIs over scraping, don't fight CAPTCHA).
- Each phase independently shippable + tested before the next.
- LinkedIn/Indeed/Glassdoor via the **JSearch aggregator adapter** can land any time
  (the `[sources].aggregator` toggle already reserves the switch).
