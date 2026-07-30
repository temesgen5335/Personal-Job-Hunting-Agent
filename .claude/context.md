# Context — Personal Job Agent

## Problem Statement

Job hunting as a software engineer is a high-volume, repetitive, multi-platform grind.
You monitor dozens of Telegram channels and job boards, manually cross-reference each
posting against your skills and preferences, tailor CVs and cover letters per company,
fill out near-identical ATS forms, and track outcomes in a spreadsheet. The process
scales linearly with effort and burns hours that should go toward interview prep.

This project automates every step except the final human decision: **apply or skip**.

## Vision

A self-hosted, single-user autonomous agent that:

1. **Ingests** job postings from Telegram channels, remote job boards (RemoteOK,
   Remotive), and ATS platforms (Greenhouse, Lever, Ashby) on a schedule.
2. **Matches** them against a structured profile (skills, roles, experience,
   location preferences) using a heuristic pre-filter (always, no API cost) plus
   an optional LLM rerank for the top candidates.
3. **Surfaces** ranked results in a Telegram bot with interactive filters
   (date/location/keywords) and one-tap apply buttons.
4. **Generates** tailored application assets (CV variant, cover letter, email draft)
   using multi-provider LLM failover — never fabricating experience.
5. **Fills** ATS forms with a HITL browser-automation flow (Playwright), pausing
   for human approval before any submit.
6. **Tracks** applications through a lifecycle (matched → drafted → submitted →
   interview → offer/rejected) on a self-hosted dashboard.

The system is **reusable by anyone** — swap `config/preferences.toml` and `.env`,
and it works for a different person with different target roles and source companies.

## End Product

Two interfaces, one backend:

- **Telegram bot** — primary daily interaction. `/menu` with filters, `/jobs`,
  `/apply <rank>` with fit-check + approval gate, `/status`.
- **Astro SSR dashboard** — analytics (funnel, rates, timeline), job detail with
  on-demand fit breakdown, application status editing, LLM/Telegram/SMTP
  configuration via an auth-gated settings page.
- **FastAPI orchestrator** — sole backend. Wraps the service layer (ingestion,
  matching, apply, fit, LLM, secrets). Both bot and dashboard are its clients.

## Current State (v2 complete, July 2026)

| Component | Status |
|---|---|
| Ingestion (6 adapters) | Done — RemoteOK, Remotive, Greenhouse, Lever, Ashby, Telegram |
| Matching (heuristic + LLM) | Done — word-boundary scoring + optional LLM rerank |
| Telegram bot | Done — interactive /menu, /apply with fit-check, ATS path |
| Tier-1 apply (email) | Done — CV tailor + cover letter + SMTP send with HITL gate |
| Tier-2 apply (ATS form-fill) | Done — Playwright fill + screenshot preview + submit gate |
| FastAPI orchestrator | Done — 15+ endpoints, auth, injectable deps |
| Dashboard | Done — overview analytics, filtered job list, job detail + fit, settings |
| Multi-LLM failover | Done — Groq/OpenRouter/Gemini/OpenAI/Anthropic + custom endpoint |
| Encrypted config UI | Done — Fernet secret store, auth-gated API, masked reads |
| Test suite | 99 tests, 17 test files, zero network, injectable fakes throughout |

## What's NOT Built Yet

- **Aggregator adapter** (JSearch/SerpApi for Indeed/LinkedIn/Glassdoor) — toggle ready, no adapter code
- **Profile/watchlist editing** in dashboard Settings (currently file-based)
- **Bot outcome-marking** (interview/offer/rejected from Telegram — currently dashboard-only)
- **Fit-checker on job list** (currently only on detail page)
- **Deployment to a live VPS** — systemd units + scripts are ready, needs a provisioned box

## Key Numbers

- **7,300+** jobs ingested in testing (across all 6 source adapters)
- **40** companies in the ATS watchlist (Greenhouse/Lever/Ashby)
- **99** tests across 17 files — all run offline, no network, no credentials
- **6** LLM providers with automatic failover (3 free, 3 paid)
