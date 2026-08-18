# Security Policy

## Reporting a vulnerability

Email **temesgen5335@gmail.com** with `[SECURITY]` in the subject, or open a
[GitHub security advisory](https://github.com/temesgen5335/personalAgent/security/advisories/new).
Please do not open a public issue for anything exploitable.

Expect an acknowledgement within a week. This is a personal project maintained in spare
time — there is no SLA, and saying so is more useful than implying one.

## What this software holds

Take this seriously when deciding where to run it. A configured instance holds:

- **Your CV and identity** — name, email, phone, full CV text (`data/`, gitignored)
- **Live credentials** — LLM API keys, an SMTP password, Telegram bot and user-account
  tokens (`.env`, plus a Fernet-encrypted store)
- **A Telethon session file** that is a logged-in Telegram *user account*, not a bot
- **Your job-search history** — every posting matched, every application, every outcome

## Threat model

**In scope:** single operator, running their own instance, on a machine they control.

**Explicitly out of scope:** multi-tenancy. There is one shared dashboard password and
one `TELEGRAM_OWNER_ID`. Do not run one instance for several people — it was never
designed for it and nothing separates their data.

## Known posture — read before deploying

### Reads are unauthenticated by default

Every non-GET route requires a bearer token derived from `DASHBOARD_PASSWORD` and fails
closed without it. **GET routes do not.** That includes `/applications` and `/followups`,
which reveal where you applied, what was rejected, and where you are interviewing.

This is safe in the default configuration — the API binds `127.0.0.1`. It is **not safe**
if you:

- set `HOST=0.0.0.0`, or
- follow the split-deploy path (`PUBLIC_JOBAGENT_API_URL`, dashboard hosted separately),
  which requires the API to be publicly reachable.

In those configurations, put the API behind a reverse proxy with authentication, a VPN,
or an IP allow-list. Opt-in read authentication is planned — see item 10 in
[docs/ROADMAP.md](docs/ROADMAP.md).

### Secrets at rest

`.env` is plaintext on disk and gitignored. The dashboard's Settings page writes to a
Fernet-encrypted store whose key (`JOBAGENT_MASTER_KEY`) lives in `.env` — so the threat
it defends against is *someone reading the store file*, not someone with your `.env`.

### Rate limiting

There is none. `/assistant/ask` costs LLM tokens and `/ingest` makes outbound requests.
Neither is bounded. Another reason not to expose the API.

## Hard safety rules

Enforced in code and tested, not merely documented:

- **No application is ever submitted without explicit human approval** (R2). Both the
  email and ATS paths stop and show you exactly what will be sent.
- **CV tailoring never fabricates** — it reframes real experience only (R1).
- **No CAPTCHA solving** (R3). Anti-bot blocks hand you a deep link.
- **The assistant has no send, approve, or delete tool.** Those are not gated; they do
  not exist (R25/R26), so there is no code path for a prompt injection to reach.

If you find a way around any of these, that is a vulnerability — please report it.

## Good practice for operators

- Set `DASHBOARD_PASSWORD` to something long and unique. Without it every write fails
  closed, which is safe but not useful.
- Generate `JOBAGENT_MASTER_KEY` with `python scripts/genkey.py`; never reuse a key.
- Keep `JOBAGENT_CORS_ORIGINS` narrow. Never `*`.
- Back up `data/` — it is the only copy of your application history — and remember the
  backup carries the same PII as the original.
