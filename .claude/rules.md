# Rules — Hard Constraints

These are non-negotiable. Every agent and developer working on this codebase must
follow them. They exist because violations have caused real bugs or safety issues.

---

## Safety & Integrity

**R1 — Never fabricate CV content.**
Tailoring reframes, reorders, and emphasizes *real* experience to match a job
description. It never invents skills, titles, dates, or employers. Every
`CVVariant` traces to `base_cv_id`. The LLM prompt enforces this explicitly.

**R2 — No submission without explicit per-job approval.**
`applications.approved_at` is set ONLY by an explicit user approval action (Telegram
button or API call). Tier 1 (email) and Tier 2 (ATS form-fill) both stop at the
HITL gate and show the user exactly what will be sent. No full auto-submit.

**R3 — Don't fight anti-bot defenses.**
On CAPTCHA or hard block, hand the user a deep link and mark the application for
manual completion. Never solve CAPTCHAs. Detection uses rendered DOM elements
(`CAPTCHA_SELECTORS`), not HTML string scanning (which false-positives on shipped JS libs).

---

## Data Integrity

**R4 — Never discard source data.**
Every adapter stores the full source payload in `JobPosting.raw`. Normalized fields
are additive, not lossy.

**R5 — Dedup by logical identity.**
Same role from multiple sources collapses to one `dedup_hash`
(`sha256(company + title + location)`). Sources annotate; they don't duplicate.

**R6 — Store is the SSoT.**
All runtime state lives in the SQLite store. The dashboard and the bot read the
same tables via the FastAPI orchestrator.

---

## Source Hygiene

**R7 — APIs over scraping.**
Prefer official/public APIs (RemoteOK JSON, Remotive JSON, Greenhouse/Lever/Ashby
board APIs). Use an aggregator (JSearch/SerpApi) for Indeed/LinkedIn/Glassdoor.
Direct Playwright scraping is last resort only.

**R8 — Conservative request rates.**
Especially Telethon (your user account) and any scraping. Rate-limit and back off;
a banned account is a dead source.

---

## Secrets & Config

**R9 — Secrets only in `.env` / environment / encrypted secret store.**
Never commit credentials. Telethon `.session` files are gitignored. The Fernet
secret store (`secrets_store.py`) encrypts at rest. Dashboard config API never
echoes secrets in plaintext (masked view only).

---

## Code Quality

**R10 — Pydantic `model_config = ConfigDict(...)` only.**
Class-based `class Config:` inside Pydantic models is banned. Fix on touch.

**R11 — No `from __future__ import annotations` in files with FastAPI dependency injection.**
Causes 422 errors in strict mode because `Request` annotations become strings.
Already hit and fixed in `feature_flag.py` — don't reintroduce.

---

## Process

**R12 — Never `git commit` without explicit user approval.**
Surface the draft message and changed files; wait for explicit go-ahead.

**R13 — No `Co-Authored-By` trailer in commits.**
Omit the line entirely. Overrides default harness behavior.

---

## Architecture

**R14 — FastAPI is the sole backend.**
Both the Telegram bot and the Astro dashboard are clients of the FastAPI orchestrator.
The bot calls service-layer modules in-process (no HTTP hop). The dashboard calls
the REST API.

**R15 — Per-request Store for SQLite thread safety.**
Every FastAPI handler and every bot worker thread opens its own `Store` instance
and closes it. Never share a Store across threads.

**R16 — Injectable everything in tests.**
FakeLLM, FakeSMTP, FakePage, temp Store. No network, no credentials, no browser
in the test suite. Tests must pass offline.

---

## Test Discipline

**R17 — All tests run offline.**
No real API calls, no real Strapi/DB, no real Telegram, no real browser. Use
`FakeLLM`, `FakePage`, `monkeypatch`, `tmp_path`.

**R18 — Settings cache reset between tests.**
`conftest.py` resets `cfg._cached = None` around every test. If a test mutates
settings, the next test must not see it.
