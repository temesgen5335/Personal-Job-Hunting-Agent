# Memory — Build History & Decisions

This file records the non-obvious decisions, constraints, and lessons learned
during development. Read this to understand *why* the code is the way it is,
not just what it does.

---

## Architecture Decisions

### Why not Hermes Agent?
The original plan used NousResearch Hermes Agent as the orchestrator (cron, memory,
LLM routing, MCP). In practice, every role Hermes would fill was covered more simply:
systemd timers for scheduling, SQLite for persistence, `MultiLLM` for provider
routing, and `preferences.toml` for profile.

**No Hermes code was ever written, and none remains.** The `[mcp]` extra and the empty
`mcp_servers/` placeholder were removed in Tier 1 because they advertised an
integration that did not exist. If an agentic layer is ever wanted, add it
deliberately — do not treat the old docs as a partially-built foundation.

### Why FastAPI + Astro, not a single full-stack framework?
The Telegram bot is the primary interface — it existed before the dashboard. FastAPI
wraps the existing Python service layer (ingestion, matching, apply) that the bot
already calls in-process. Astro was chosen for the dashboard because it generates
minimal JS, renders server-side, and doesn't require a JS framework (React/Vue/Svelte).
The dashboard is a thin read/config layer over the API, not an application in itself.

### Why SQLite, not Postgres?
Single-user self-hosted system. SQLite is zero-config, zero-process, and the entire
store is one file that backs up with `cp`. The per-request open/close pattern handles
FastAPI's threadpool. If multi-user or concurrent writes ever matter, swap to Postgres
— the Store interface is the same.

### Why multi-LLM failover?
Free-tier API keys (Groq, Gemini, OpenRouter) have aggressive rate limits and quotas.
A single provider goes down mid-digest. The `MultiLLM` chain tries each in order and
falls through on any error. Adding a paid provider just moves it to the front of the
chain; free ones remain as backups. Zero-config resilience.

### Why Fernet for the secret store?
The dashboard needs to persist LLM keys so users don't edit `.env` files on a VPS.
Fernet (symmetric, authenticated encryption) is stdlib-adjacent (`cryptography` package),
has no key-management complexity, and encrypts at rest. The master key lives in `.env`
(or environment), so the threat model is: if someone reads the encrypted file from disk,
they can't extract the secrets without the key.

---

## Process Constraints

### The two-Telegrams rule
Telegram plays two unrelated roles:
1. **Reading channels** — Telethon (MTProto, logs in as a user account). Needs `api_id`,
   `api_hash`, `phone`, and a persisted `.session` file with a stable IP.
2. **Interactive bot** — Bot API (BotFather token). Stateless HTTP, runs anywhere.

Never conflate them. The reader is in `ingestion/adapters/telegram.py`; the bot is in
`bot/`. Telethon requires a VPS with persistent storage and a stable IP (rotating IPs
get the user account flagged). Bot API works from anywhere including serverless.

### HITL gate is non-negotiable
Every submission path (email Tier 1, ATS Tier 2) shows the user exactly what will be
sent and waits for explicit approval. This is not a convenience feature — it's a safety
constraint (R2). The `approved_at` timestamp is the audit trail.

### CV tailoring vs. fabrication
The LLM prompt for `tailor_cv()` explicitly instructs: reframe and emphasize real
experience, never invent. The output traces to `base_cv_id`. This is a legal and
ethical constraint (R1), not just a preference.

---

## The July 2026 audit

A skeptical audit of the whole system found the core pipeline real and working, but
three systemic problems worth remembering, because they are the shape of what goes
wrong here:

1. **Two auth models, one enforced.** The bot had a real owner-gate; the API — which
   by v2 could do everything the bot could — had auth only on `/config`. Every
   mutation finding traced to this. `/apply/{id}/approve` answered **200** to an
   anonymous caller. Fixed in Tier 1 (R19), with a route-table test as the net.
2. **Documentation lagged the code by a full major version.** README claimed v1 /
   72 tests / "read-only dashboard"; `pyproject.toml` and `ARCHITECTURE.md` claimed
   Hermes Agent orchestration and MCP tool servers that had never been written. The
   lesson: aspirational docs age into lies. Fixed in Tier 1.
3. **Failures were logged but never surfaced.** The `events` table captured errors
   nothing ever read, and stdout was the only alert channel. Fixed in Tier 1 via
   `pipeline_health()` + digest banner.

The fourth — **git history retained the PII and CV** — was closed on 2026-07-30:
`git filter-repo` removed the CV PDF blob from every tree and replaced the phone/email
strings in `config/preferences.toml`'s history with the same placeholders the tracked
file uses. All 37 commits preserved (rewritten SHAs — this table was remapped), zero
PII in any blob afterward, verified by full-history content scans. A pre-scrub backup
bundle exists at `~/repos/personalAgent-pre-scrub-backup.bundle` (contains the PII —
that is its job; delete after verifying the pushed repo). Note: a history rewrite
invalidates every SHA recorded in docs — remap them in the same change, as done here.

## The R1 near-miss (Tier 2, July 2026)

Tier 2 verification against the *real* LLM found two fabrication bugs that a fully
green 172-test suite had not:

- `draft_email` — the email actually sent to employers — claimed "over 8 years of
  experience" for a candidate with 3+, and asserted three technologies absent from the
  CV. Cause: `email_prompt` passed the job ad but no CV, so the employer's requirement
  list was the only material available and the model mirrored it back.
- `draft_followup` invented "over 5 years of experience" for the same reason, plus a
  literal `[date of application, 11 days ago]` placeholder in sendable text.

Also found: models emit literal newlines inside JSON strings, so strict `json.loads`
failed and the fallback returned the **entire raw JSON blob as the email body**. An
employer would have received `{"subject": ...}`.

Two durable lessons, now rules R1a/R1b:
1. Any generator that describes the candidate must receive the CV, or be forbidden from
   making claims at all.
2. FakeLLM tests verify plumbing, never truthfulness. Prompt changes need a live run
   against a job ad the CV does not satisfy — that is the only way this class of bug
   becomes visible.

## The eval harness's first catch (Tier 3)

Built to guard ranking quality, it caught a live defect on its first run: a
"Junior AI Engineer (Internship)" posting with perfect skill hits scored 0.580 and
outranked the genuine "Founding Engineer" positive (0.405). The flat -0.20 seniority
penalty could not beat a full role+skills stack. Fix: a level-mismatched title is not
the target role, so the mismatch now dampens the role component itself (×0.3) on top
of the penalty. Lesson: unit tests assert behaviors; only a ranked, labeled dataset
asserts *quality* — and floors belong at measured reality, with known misses
documented in the dataset rather than hidden by generous thresholds.

## Free-model capability spike (Aug 2026)

Measured against the real CV and store before changing the Groq default, because the
agentkit plan assumed the 8B model was too weak and assumptions about models age badly.

| | quality (existing tasks) | latency / scoring call | tool loop |
|---|---|---|---|
| `llama-3.1-8b-instant` | 18/18 | **0.38s** (fastest) | **0/5** |
| `llama-3.3-70b-versatile` | 18/18 | 0.61s | 5/5 |
| `openai/gpt-oss-120b` | 18/18 | 1.26s | 5/5 |

Three things worth keeping:

1. **The 8B emits perfect tool calls and still cannot run a loop.** 5/5 on emitting a
   well-formed call and 5/5 on picking the right tool, then 0/5 on using the tool
   *result* to answer. A naive capability probe would set `native_tools=True` and be
   badly wrong — which is exactly why the plan says a probe may set capability flags
   but never tier.
2. **A first latency measurement was off by 30x and nearly shipped in a code comment.**
   Bursting three tasks x six runs hit Groq rate limits, and the OpenAI SDK retries
   internally with backoff, so the numbers measured queueing rather than the model. Fair
   A/B needs warmed clients, interleaved models, and spacing between calls.
3. `llama-3.3-70b` over-calls tools on a conversational closing ("thanks, that's all")
   but answers three other no-tool prompts directly. `gpt-oss-120b` was clean 20/20 and
   is the better pure-capability pick, at ~3x the latency.

## The agent loop, proved end to end (Aug 2026)

Phase 1 of agentkit closed with the strategy executors and the `Runner`. Three things
the offline suite could not have told us, all found by making real calls:

1. **The degradation claim is real, not theoretical.** The same task —"when did the
   pipeline last run?"— answered correctly twice: through `native_loop` on
   `llama-3.3-70b-versatile`, and through `prefetch_single_shot` on
   `llama-3.1-8b-instant`, the model measured at 0/5 on tool loops. Python ran the
   retrieval; the 8B only wrote the sentence. That is the whole design justified in one
   comparison.
2. **The shipped OpenRouter default was dead.** `meta-llama/llama-3.3-70b-instruct:free`
   now 404s — OpenRouter moved it behind the paid slug — so the third provider in the
   chain would have failed on every call, silently, forever. Nothing offline could catch
   this; a live call caught it immediately. Replaced with `openai/gpt-oss-20b:free`
   (~12s, verified). **`:free` slugs are withdrawn without notice — re-verify the
   default whenever the chain looks short.**
3. **The full failover walk works on real errors.** Groq 401 → classified PERMANENT,
   breaker opened, never retried. Gemini 429 with no `Retry-After` → switched rather
   than waiting. OpenRouter answered, and the router *changed strategy* on the way
   because that backend's tool support is unproven. Failover crossed a capability
   boundary without the caller knowing.

Also worth keeping: the `agentkit` vocabulary boundary test earned its place. It failed
on the word "employer" in a `jsonx.py` docstring — a comment, not code. That is exactly
the slow leak the test exists to catch, and it caught it on the first try.

Gemini's tool support is *still* unmeasured: every attempt has hit the free-tier quota.
Its card says `native_tools=True` from the family pattern and its `notes` field says
"unverified". Leave that honest until a call actually succeeds.

## Phase 2: what the tests caught that review did not (Aug 2026)

The governed tool seam landed with permission tiers, an FTS5 index, a fail-closed audit
trail and `GuardedToolBox`. Three things are worth remembering, and none of them came
from writing the code — they came from trying to break it afterwards.

1. **A safety test passed with its own property broken.** The first version of
   "intent is recorded before the policy runs" asserted the *order of audit events*.
   Moving `intent()` to sit after the policy check still produces intent-then-decision
   in the sink, so the test stayed green while the property was gone. What actually
   pins it: a dead sink must stop the call *before the gatekeeper is asked anything*,
   proven with a spy gate. **Assert the consequence, not the sequence** — a test that
   only observes ordering of side effects is often observing nothing.
2. **The import cycle bit twice.** `agentkit.tools` imports `agentkit.llm.types`, which
   executes `agentkit/llm/__init__.py`, which imported the Runner, which imported
   `agentkit.tools`. It only failed depending on which module was imported first, so it
   looked fixed once. The real fix was better design: the Runner is duck-typed against
   the seam (`specs()`/`execute()`) instead of the concrete class — which is exactly why
   a governed box can substitute for a plain one. Now guarded by a topological-sort test
   that counts module-level imports only (a lazy import inside a function is the
   legitimate escape hatch, and `chain.py` uses one).
3. **General-purpose tools are excluded for every host, not per host.** `execute_sql`,
   `run_shell`, `http_fetch`, `eval`, filesystem access — dangerous because they are
   *general*: any one of them collapses every other restriction into a suggestion.
   `PolicyBook` unions `UNIVERSALLY_EXCLUDED` into whatever the host passes, so a host
   cannot drop them by overwriting the field. Codified as R25-R29.

Every adversarial check was run in both directions: break the property, confirm the
test goes red, restore, confirm green. The one that did not go red got rewritten.

## Known Limitations

- **No LinkedIn/Indeed/Glassdoor adapter.** These sites are aggressively anti-bot with
  no public API. The planned approach is a third-party aggregator (JSearch via RapidAPI).
  The `Sources.aggregator` toggle and `Source.aggregator` enum are ready; only the
  adapter code is missing.
- **Profile editing is file-based.** `config/preferences.toml` must be edited manually
  or via SSH. Dashboard Settings currently only covers LLM/Telegram/SMTP configuration.
- **No multi-user support.** The system is designed for one person. TELEGRAM_OWNER_ID
  gates bot access. Dashboard auth is a single shared password.
- **Playwright needs a real browser.** `playwright install chromium` must run on the
  deployment target. On ARM (Oracle Cloud), Chromium works but is heavier. ATS form-fill
  is optional — the system is fully functional without it.

---

## Version History

| Version | Commit | What changed |
|---|---|---|
| v1.0 | `8b68ebb` | Phase 0–4 complete: ingestion, matching, bot, Tier-1+2 apply, VPS deploy |
| v2.0 | `6b8c007` | FastAPI orchestrator; dashboard fetches API instead of direct SQLite |
| v2.1 | `3fed58c` | Encrypted config UI, auth-gated settings API, custom LLM provider |
| v2.2 | `7c5eea9` | Fit-checker (confidence score + explainable report) |
| v2.3 | `5746355` | Application tracker + analytics (funnel, rates, timeline) |
| v2.4 | `546c5fe` | Job detail pages, on-demand fit check, inline charts |
| v2.4+ | `aeee964` | Portfolio theme, header fix, location filters, fit breakdown, pagination |
| gov | `eb3e34b` | `.claude/` governance dir + `CLAUDE.md`/`AGENTS.md` entrypoints |
| gov | `86a2c20` | Makefile runner (install / check / run) |
| Tier 1 | `c876038`..`ba4e5f8` | Audit remediation: API auth on writes, CORS default, browser API URL, retry/backoff, pipeline health, PII split, docs truth-pass |
| Tier 2 | `a41c592`..`ebf4567` (pre-scrub SHAs; rewritten) | Weighted matching, R1 email grounding, status lifecycle, follow-up drafts, gap chips |
| scrub | `6a458d0` | History rewrite: CV blob + phone/email removed from all 37 commits |
| Tier 3 | `65c4afc`.. | Run-ID spine + run ledger, matching eval harness (P@5=1.0 floor), honest ARCHITECTURE.md |
| agentkit P1 | `3166413`.. | Capability-aware multi-LLM: IR, tier registry, breaker, router, nine strategies, Runner. 386 tests |

---

## Deployment History

- **GitHub Actions** — `.github/workflows/digest.yml` runs daily digest from API sources
  (no Telethon). Works on the free tier. Already deployed and tested.
- **VPS (systemd)** — `deploy/` has service/timer units, `scripts/install_services.sh`
  substitutes paths. Designed for Oracle Cloud Always Free ARM VM. Not yet deployed
  to a live box.
- **Dashboard** — Astro SSR, targeted for Vercel free tier or same VPS behind Caddy.
