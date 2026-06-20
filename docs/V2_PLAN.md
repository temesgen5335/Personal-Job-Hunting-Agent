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

### v2.0 — FastAPI orchestrator (foundation)
- Extract a service layer; FastAPI app with REST endpoints: jobs, matches,
  applications, stats, trigger ingest/match, prepare/approve apply, ATS preview/submit.
- Bot refactored to call the API (or shared service); dashboard reads via API.
- Keep SQLite as SSoT; FastAPI is the sole mutator (restores R40-style discipline).
- **Exit:** bot + dashboard both run against one backend; tests green.

### v2.1 — Config UI + secret management
- Auth on the dashboard (single admin password/token — it now edits secrets).
- **Encrypted secret store** (gitignored file or OS keyring), edited via UI:
  LLM provider + keys, Telegram tokens, SMTP, sources toggles, watchlist, profile.
- **Custom OpenAI-compatible provider** (Ollama/vLLM/any base_url) in the failover chain.
- Security note: secrets-in-UI is acceptable for a self-hosted single-user box behind
  your own network; the auth gate + encryption-at-rest are mandatory, not optional.
- **Exit:** a fresh user configures everything from the dashboard, no file editing.

### v2.2 — Fit-checker (confidence score + explainable report)
- A dedicated fit model run at apply-time and shown in bot + dashboard:
  - **Keyword/ATS coverage:** % of the JD's key terms present in the CV.
  - **Requirement match:** LLM extracts JD requirements → matched / partial / missing.
  - **Experience match:** seniority + years vs. the role.
  - **Confidence %** + a short "why" and the top gaps to address.
- Surfaced as "You're ~78% fit — missing: Kubernetes, on-call experience" before you apply.
- **Exit:** every job in /apply and the dashboard shows a fit score + breakdown.

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
