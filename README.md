# Personal Job Agent

A personal, autonomous job-hunting agent. It ingests job postings from Telegram
channels and job boards, scores them against your CV and preferences, and helps you
apply — drafting tailored CVs, cover letters, and emails, and (with your approval)
filling application forms. It runs on a VPS, scheduled and autonomous, and you drive
it through a **Telegram bot**. A tracking dashboard comes later.

Orchestrated by [Hermes Agent](https://github.com/NousResearch/hermes-agent) (the
brain: scheduler, memory, LLM routing, MCP tools). This repo is the **tools and
domain logic** Hermes drives — not a from-scratch agent loop.

## Status: Phases 0–3 complete (50 tests passing)

| Layer | State |
|---|---|
| Phase 0 — schemas · SQLite store · config | ✅ |
| Phase 1 — 6 ingestion adapters (RemoteOK, Remotive, Greenhouse, Lever, Ashby, Telegram) + runner | ✅ |
| Phase 2 — matching engine (heuristic + LLM rerank) + Telegram bot digest | ✅ |
| Phase 3 — Tier-1 apply (CV tailor, cover letter, email) + `/apply` HITL button | ✅ |
| Multi-provider LLM with failover (Groq/OpenRouter/Gemini/OpenAI/Anthropic) | ✅ |
| Deployment — VPS systemd + GitHub Actions | ✅ artifacts ready |
| Phase 4 — Tier-2 HITL ATS form-fill (Playwright) | ⏳ next |
| Phase 5 — tracking dashboard | ⏳ |

See [docs/PLAN.md](docs/PLAN.md) for the full build plan and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design. Deploying:
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) (VPS, primary) and
[docs/DEPLOYMENT_ALTERNATIVES.md](docs/DEPLOYMENT_ALTERNATIVES.md) (free/PaaS options compared).

## Quickstart (local)

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env        # fill in credentials later
python scripts/init_db.py   # create the SQLite store
pytest                      # run smoke tests
```

## Hard rules
See [.agent/rules.md](.agent/rules.md) — never fabricate CVs, never submit without
per-job approval, prefer APIs over scraping, secrets never in git.
