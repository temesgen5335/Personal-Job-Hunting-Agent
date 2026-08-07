# Rules — Hard Constraints

These are non-negotiable. Every agent and developer working on this codebase must
follow them. They exist because violations have caused real bugs or safety issues.

---

## Safety & Integrity

**R1 — Never fabricate CV content.**
Tailoring reframes, reorders, and emphasizes *real* experience to match a job
description. It never invents skills, titles, dates, or employers. Every
`CVVariant` traces to `base_cv_id`. The LLM prompt enforces this explicitly.

**R1a — Every generator that makes claims must receive the CV.**
A prompt containing the job ad but no CV leaves the employer's requirements as the
only facts available, and models assert them as the candidate's. A real run of
`draft_email` produced "over 8 years of experience" (actual: 3+) and named three
technologies absent from the CV — in the email that gets *sent*. Either pass
`cv_master_md`, or forbid claims outright as `FOLLOWUP_SYSTEM` does. Never add a
generator that describes the candidate from the job ad alone.

**R1b — Prompt guardrails are load-bearing; test them.**
The anti-fabrication clauses are asserted in `tests/test_apply.py` and
`tests/test_followups.py`. FakeLLM tests cannot catch fabrication — only a real model
run can — so when changing a generator prompt, run it live against a job ad whose
requirements the CV does NOT satisfy and check what it claims.

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

**R4a — The ingest gate may drop whole postings, but only on stable axes.**
R4 protects *fields within* a job you keep; the ingest gate decides whether to keep a
posting at all, which is a different axis — and unlike scoring, it is irreversible. So
the gate is limited to facts the user will not change their mind about (age, location,
hard-exclusion keywords) and must never filter on fit judgment (skills, seniority,
requirements): the scorer handles those reversibly and for free, so a preference change
re-ranks the existing store instead of needing a re-fetch. Every drop is counted per
reason in the ingest event and run summary — a mis-set gate must read as "481 filtered",
never as an empty queue.

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

## Application Lifecycle

**R23 — Status changes follow `ALLOWED_TRANSITIONS`.**
An application is a real-world process: you cannot un-submit, and `offer` cannot revert
to `matched`. Invalid moves are refused with 422 naming the legal set. A deliberate
correction passes `correction=true`, which bypasses the map and logs a
`status_correction` event — possible, but never silent. `allowed_next` ships with each
row so the UI never duplicates the map.

**R24 — Follow-ups are drafts only. There is no send path.**
`draft_followup` returns text; nothing in `generators.py` can reach a mailer, and
`tests/test_followups.py` asserts that structurally. The user sends nudges personally.
Do not add a send endpoint for follow-ups.

---

## Agent Harness (agentkit)

**R25 — No general-purpose tool exists at any permission tier, ever.**
No `execute_sql`, `run_shell`, `http_fetch`, `eval`, or filesystem read/write tool may
be registered for any agent. These are dangerous not because of what they do but
because they are *general*: one of them collapses every other restriction into a
suggestion — excluded tools become reachable through it, frozen config becomes
writable, and the audit trail records one opaque line instead of the action that
actually happened. Expose narrow, named operations instead: `set_max_age(days)` can be
reviewed on a confirmation card; `execute_sql(query)` cannot. Enforced structurally by
`UNIVERSALLY_EXCLUDED` in `agentkit/permissions.py`, which `PolicyBook` unions into
every host's exclusion set so a host cannot drop it by overwriting the field.

**R26 — Excluded means absent, not gated.**
A tool the agent must never have does not get a policy that denies it — it is never
registered. A gate is a runtime check an attacker has to defeat once; absence has no
code path to attack. `PolicyBook.guard()` turns an attempt to register one into an
exception at wiring time. This is the same call R24 already made for follow-ups.

**R27 — Intent is audited before the policy runs, and audit failure aborts the call.**
`tool_intent` is written first, so a *refused* attempt survives in the trail — that is
the line you most want after something goes wrong. If the sink raises, the tool does
not run: an agent that keeps acting while its trail is broken produces actions nobody
can reconstruct. Note that asserting event *order* does not hold this property; the
test must show the policy was never consulted.

**R28 — The policy input carries no transcript and no retrieved text.**
`SessionContext` holds the actor, the surface and prior grants — nothing the model
wrote and nothing retrieval returned. Fences and provenance labels reduce the odds a
model is *talked into* asking for something; they cannot make it impossible. So the
component that answers "may this run?" is given no access to the text doing the
talking. An injection can make the model request a config rewrite; it cannot make the
gatekeeper approve one.

**R29 — Confirmations are server-side, single-use, and bound to the exact arguments.**
The nonce is minted server-side and never derived from model output. It expires, it is
consumed on sight, and it carries `sha256` of the arguments — because the interesting
attack is not getting a dangerous tool approved, it is getting a harmless one approved
and then changing what it does. Confirmation cards render from validated arguments and
computed diffs, never from model prose.

---

## API Security

**R19 — Every state-changing API route is auth-gated, and fails closed.**
Any route that is not GET carries `dependencies=auth` (the `require_auth` bearer-token
check derived from `DASHBOARD_PASSWORD`). With no password set, writes return 403
rather than running unauthenticated. These routes send email and submit forms as the
user, so port reachability must never equal authority. `/auth/login` is the sole
exception. `tests/test_api.py::test_every_mutating_route_requires_auth` enumerates the
route table and fails if a new write route forgets the gate — do not weaken it.

**R20 — `JOBAGENT_CORS_ORIGINS` never defaults to `*`.**
An open origin plus a reachable port is how a stranger drives the apply endpoints.
Deployments name their dashboard origin explicitly.

---

## Source Hygiene (implementation)

**R21 — All source HTTP goes through `get_with_retry`.**
Bounded exponential backoff with full jitter; retries 429/5xx and transport errors;
treats other 4xx as permanent (a wrong company slug must not burn three attempts);
honors a capped `Retry-After`. Never call `client.get` directly in an adapter — that
is what made R8 an unmet claim for the whole of v1/v2.

---

## Personal Data

**R22 — The committed config carries placeholders only.**
Identity (name, email, phone, cv_path, links) lives in `config/preferences.local.toml`,
which is gitignored and overlaid section-wise at load time. The CV PDF is gitignored.
`tests/test_preferences.py::test_committed_preferences_carry_no_personal_contact_details`
guards against regression. This repo is also a portfolio piece.

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
