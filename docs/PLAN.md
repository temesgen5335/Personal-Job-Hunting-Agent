# Build Plan

Each phase is independently shippable. You get real value at Phase 2 (daily ranked
digest in Telegram) before any auto-apply exists.

Decisions locked in:
- **Auto-apply**: Tier 1 (email) + Tier 2 (HITL ATS form-fill). No full auto-submit.
- **Hosting/LLM**: VPS + OpenRouter API.
- **Sources**: Telegram channels, RemoteOK, Remotive, Greenhouse/Lever/Ashby,
  aggregator for Indeed/LinkedIn/Glassdoor/JobRight, Playwright fallback.

---

## Phase 0 — Foundations ✅ (this scaffold)
- [x] Repo + git, package layout under `src/jobagent/`
- [x] Core schemas: `JobPosting`, `Match`, `Application`, `CVVariant`, `Event`
- [x] SQLite store + `schema.sql` + `init_db.py`
- [x] Config via pydantic-settings + `.env.example`
- [x] Base ingestion adapter interface
- [x] Smoke tests + hard rules (`.agent/rules.md`)
- [ ] VPS: install Hermes Agent, `hermes setup` → OpenRouter, `hermes gateway install`
      (run `scripts/vps_setup.sh` on the box)

## Phase 1 — Ingestion adapters → store
- [x] Adapter run loop: fetch → dedup → upsert → log events (`ingestion/runner.py`)
- [x] RemoteOK adapter (free JSON) — pipeline proven live (100 jobs ingested)
- [x] Remotive adapter (free JSON) — verified live (30 jobs)
- [x] Greenhouse / Lever / Ashby adapters (config-driven slugs) — all verified live
      (Stripe 512 / matchgroup 73 / Ramp 114)
- [x] Telegram channel reader (Telethon, first-run login, session file) — parser
      unit-tested; live run needs your api_id/api_hash + channels + `.[telegram]` extra
- [ ] Aggregator adapter (SerpApi Google Jobs → Indeed/LinkedIn/Glassdoor/JobRight)
- [ ] Playwright fallback adapter (last resort, per-board)
- [ ] Wire fetch loop to Hermes cron
- **Exit:** scheduled pulls populate the store from all live sources; deduped; idempotent.

## Phase 2 — Matching + Telegram digest *(first payoff)* ✅
- [x] CV + preferences → structured profile (config/preferences.toml + cv_master.md)
- [x] Matching: heuristic scorer (always) + optional OpenRouter LLM reranker;
      verified live on 7,331 jobs (199 strong ≥70%)
- [x] Telegram bot (Bot API): `/start` `/help` `/jobs [N]` `/status`, owner-locked
- [x] Digest push script for Hermes cron (scripts/send_digest.py)
- **Exit:** ranked digest available via /jobs and pushable to Telegram. ✅

## Deployment — VPS (systemd) ✅ artifacts ready
- [x] Unified pipeline (ingest→match→digest) + shared adapter registry
- [x] systemd units: bot service, ingest timer (4h), pipeline/digest timer (daily)
- [x] install_services.sh (path substitution) + vps_setup.sh bootstrap
- [x] docs/DEPLOYMENT.md runbook; verified locally (pipeline run + unit render)
- [ ] Run on an actual VPS (needs a provisioned box) — follow docs/DEPLOYMENT.md
- Note: Hermes Agent installed separately as the Phase 3 agentic brain.

## Phase 3 — Application assets (Tier 1)
- [ ] `cv_tailor` MCP tool (reframes real experience only — R1)
- [ ] `cover_letter` + `email_draft` tools
- [ ] Bot flow: tap job → review assets → approve → send email (SMTP/Gmail)
- [ ] Log to `applications` + `events`; stamp `approved_at`
- **Exit:** apply to email-based postings end-to-end with one approval.

## Phase 4 — HITL form-fill (Tier 2)
- [ ] `apply_executor`: Playwright fill for Greenhouse/Lever/Ashby layouts
- [ ] Pause + screenshot before submit → Telegram approval → submit
- [ ] CAPTCHA / hard-block → hand back deep link (R3)
- **Exit:** one-approval assisted submission on the three ATS platforms.

## Phase 5 — Tracking dashboard
- [ ] Next.js read-only dashboard over the same store
- [ ] Funnel, status, history, source analytics
- **Exit:** web view; Telegram stays primary.

---

## Risk register
- **Telegram ToS**: Telethon user-account reading is a gray area — keep rates low,
  consider a dedicated account. (R8)
- **ATS ToS / anti-bot**: Tier 2 carries account risk; HITL + no CAPTCHA-solving. (R2, R3)
- **CV integrity**: never fabricate. (R1)
- **Aggregator cost**: SerpApi ~$50/mo; cap query volume.
