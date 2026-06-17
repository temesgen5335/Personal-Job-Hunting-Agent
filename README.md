# Personal Job Agent

A personal, autonomous job-hunting agent. It ingests job postings from Telegram
channels and job boards, scores them against your CV and preferences, and helps you
apply — drafting tailored CVs, cover letters, and emails, and (with your approval)
filling application forms. It runs on a VPS, scheduled and autonomous, and you drive
it through a **Telegram bot**. A tracking dashboard comes later.

Orchestrated by [Hermes Agent](https://github.com/NousResearch/hermes-agent) (the
brain: scheduler, memory, LLM routing, MCP tools). This repo is the **tools and
domain logic** Hermes drives — not a from-scratch agent loop.

## Status: Phase 0 (foundations)

| Layer | State |
|---|---|
| Core schemas (`JobPosting`, `Match`, `Application`, `CVVariant`, `Event`) | ✅ |
| SQLite store + schema | ✅ |
| Config (pydantic-settings) | ✅ |
| Ingestion base adapter | ✅ |
| Smoke tests | ✅ |
| Ingestion adapters | ⏳ Phase 1 |
| Matching + Telegram digest | ⏳ Phase 2 |
| Application assets (Tier 1) | ⏳ Phase 3 |
| HITL form-fill (Tier 2) | ⏳ Phase 4 |
| Dashboard | ⏳ Phase 5 |

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
