# Kickoff brief — MCP server for personalAgent (fresh-session prompt)

> **You are a fresh session.** You have none of the prior conversation's context. This
> doc is your complete starting point. **Investigate first, then design, then plan — do
> not build anything until a design is approved.** Use the `superpowers:brainstorming`
> skill (architectural path) → spec → `superpowers:writing-plans`. Load the `claude-api`
> skill and current MCP/Anthropic docs before designing the protocol surface.

---

## 1. The target

Build an **MCP (Model Context Protocol) server** that lets a coding agent (Claude Code,
Codex, etc.) **operate personalAgent naturally, mid-work**, on this project. Two jobs:

1. **One-command onboarding** — an agent opening this repo can set the system up in one
   go (this pairs with the CLI onboarding built in the public-readiness spec; see §5).
2. **Operate the running system** — once set up, an agent should, through the MCP, be
   able to: **pull jobs**, **run/refresh matching**, **read and sort the ranked matches**,
   **triage and track applications**, **read the user's store / settings / preferences /
   history**, and **research specific roles and companies** — always following the
   system's real process, and with **every decision and action recorded on the audit /
   run-ledger for later debugging**.

The point is that the coding agent becomes a first-class *operator* of the system, using
the same governed process a human would, not a side channel that bypasses it.

## 2. What personalAgent is (one paragraph)

A self-hosted, single-user autonomous job-hunting agent. It ingests postings from 7
sources (RemoteOK, Remotive, Himalayas, Greenhouse, Lever, Ashby, Telegram; + a key-gated
JSearch aggregator), matches them against a structured profile (a transparent heuristic
scorer + optional LLM rerank, with config-driven geographic eligibility), surfaces ranked
results, generates tailored application assets (CV / cover letter / email) **without
fabricating experience**, and applies with a **human-in-the-loop gate** (email + ATS
form-fill) — tracking outcomes through a lifecycle. Three surfaces (FastAPI orchestrator,
Telegram bot, Astro dashboard) sit on one shared service layer. Read `CLAUDE.md`,
`.claude/agent.md`, `.claude/context.md`, and `.claude/rules.md` first.

## 3. The load-bearing insight: most of this already exists — reuse it

**Do not build a new access path.** personalAgent already has a **governed tool system**
built exactly for "an agent operates the system safely." The MCP server should be a
**thin adapter** over it.

- **`src/jobagent/assistant/`** — the domain half: ~15 in-process tools (`tools.py`,
  `ToolSpec` + `ToolPolicy`), `CONFIG_WRITABLE` allow-list, impact-preview dry-runs over
  real rows, config snapshots + rollback, FTS5 search over postings fenced as UNTRUSTED.
  R2 exclusions exist as *absences* (there is no send/approve/ATS tool). Interfaces today:
  `scripts/ask.py` (CLI), the `/assistant` dashboard page, Telegram `/ask` — all one
  mechanism, three renderers. **The MCP is the fourth renderer.**
- **`src/agentkit/guard.py`** — `GuardedToolBox`, same shape as `ToolBox`, so there is
  **no ungoverned path**. Fixed order inside `execute()`: audit intent → allow-list →
  policy → audit decision → run → audit result. Permission tiers READ / ACT / ADMIN,
  argument-bound single-use confirmations, fail-closed audit on the `run_id` spine. This
  is your "track every decision for debugging" requirement, already built.
- **`src/jobagent/api/app.py`** — FastAPI `create_app()` factory; endpoints for stats,
  jobs/matches, ingest, runs (the ledger: `GET /runs`, `GET /runs/{id}`), triage, profile,
  config, purge, health, fit. Bearer auth from `DASHBOARD_PASSWORD`; reads open by
  default, writes fail-closed (R19).
- **`src/jobagent/store/db.py`** — SQLite, `get_matches`, `stats`, events/run-ledger,
  triage, applications. Per-request open/close.
- **Config:** `config.py` (Settings), `preferences.py` (profile/watchlist/sources +
  geo config), the Fernet secret store.

**Your core design question is the mapping:** does the MCP expose the *assistant's
governed toolbox* directly as MCP tools (preferred — reuses permissions + audit), the
*REST API*, or a curated blend? Read the assistant toolbox and `guard.py` before deciding.

## 4. Hard constraints (non-negotiable — these are project rules R1–R32)

- **R25** — never register a general-purpose tool (`execute_sql`, `run_shell`,
  `http_fetch`, filesystem). Expose *specific, governed* capabilities only.
- **R26** — a forbidden capability is an *absence*, not a gated tool. Don't add a
  send/auto-submit tool "but gated." There is deliberately no such tool.
- **R27** — audit intent *before* policy runs; if the audit sink fails, fail closed.
- **R28 / R29** — no transcript/model output on `SessionContext`; confirmations are
  server-side and argument-bound, never trusted from the caller.
- **R2 (HITL)** — the MCP must **never** auto-submit an application. Drafting/preview is
  fine; the send/submit stays a human action. This is the whole product's spine.
- **R1** — no CV fabrication; tailoring reframes real experience only.
- **R30** — `agentkit` must not import `jobagent`, FastAPI, or a provider SDK. If the MCP
  server lives in/near agentkit, keep that boundary; more likely it lives on the jobagent
  side as an adapter over the assistant toolbox.
- **R17** — no real network/APIs in tests; use the injectable fakes (`FakeLLM`, `FakePage`,
  `TestClient`, `tmp_path` stores) already in the suite.

Everything the MCP exposes must flow through the existing governed `execute()` so the
run-ledger captures it — that is the debugging story, don't reinvent it.

## 5. Fit with current & future builds (context you must respect)

- A **public-readiness spec** is in flight (`docs/superpowers/specs/2026-09-12-
  personalagent-public-ready-design.md`). It builds: keyless first-run, demo-into-store
  seeding, **CLI onboarding** (`make quickstart` + `make onboard`), and **presentation-
  layer scaffolding** (clean, tested API/service contracts so a future dashboard is
  "just painting a frontend"). **The MCP should consume those same service/API contracts**
  — not a parallel stack. Coordinate with, don't duplicate, that work.
- **Onboarding-for-agents (your §1.1)** overlaps with that spec's `make onboard`: aim for
  a **non-interactive/scriptable** onboarding an agent runs in one command, plus an
  `AGENTS.md` (and a note in `CLAUDE.md`) telling an agent to onboard first.
- **Dashboard work is deferred** globally right now. Your MCP is backend/protocol only.
- **Per-task LLM model selection** (different model per agentic step) is a *later*
  enhancement, not built yet — don't assume it exists.

## 6. Open questions for you to resolve (in your investigation + spec)

1. **Transport:** stdio (local, per-project, simplest for a coding agent on the repo) vs.
   HTTP/SSE (for a running server). Recommend stdio-first given the single-user, local,
   self-hosted model — but verify.
2. **Tools vs resources:** which capabilities are MCP *tools* (actions: pull, triage,
   draft) vs MCP *resources* (readable context: current matches, profile, settings,
   run history, a role/company research dossier)?
3. **Auth:** how the MCP authorizes writes given R19 (`DASHBOARD_PASSWORD`) — does the
   MCP hold the token, run in a trusted local context, or map to ACT/ADMIN tiers?
4. **Process fidelity:** how the MCP makes an agent *follow the lifecycle*
   (`ALLOWED_TRANSITIONS`, the HITL gate) rather than around it.
5. **"Research a role/company":** is this a new governed tool (web/company research) or a
   composition of existing read tools + the assistant? Mind R25 (no general `http_fetch`).
6. **Debugging surface:** how an agent (and the user) later reads back what the MCP did —
   reuse `GET /runs/{id}` and the audit trail; consider an MCP resource that exposes it.

## 7. Deliverables expected from your session

1. An **investigation report** — what exists (assistant toolbox, guard, API, ledger),
   what the MCP can reuse verbatim, and the true gap.
2. A **design spec** (via brainstorming) at `docs/superpowers/specs/<date>-mcp-server-
   design.md`, covering the tool/resource surface, transport, auth, audit, and the reuse
   of the governed toolbox — checked against R1/R2/R25–R30.
3. An **implementation plan** (via writing-plans) once the spec is approved.

Do **not** implement before the spec and plan are approved by the user (R12: no commits
without explicit approval; R13: no `Co-Authored-By` trailer).
