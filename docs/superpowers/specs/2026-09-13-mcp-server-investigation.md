# Investigation — MCP server for personalAgent

- **Date:** 2026-09-13
- **Status:** Report (deliverable 1 of the kickoff brief). Not committed pending review (R12).
- **Companion:** `2026-09-13-mcp-server-brief.md` (the ask), `2026-09-13-mcp-server-design.md` (the design), `2026-09-12-personalagent-public-ready-design.md` (the parallel onboarding/contracts spec).
- **Method:** every claim below was read off the code (`src/`, `tests/`, `scripts/`, `Makefile`, `.claude/`), the installed `mcp` 2.2.0 package (introspected in a scratch venv), or the current MCP spec pages. Nothing is from memory — this project has paid for guessed interfaces four times (memory.md, "a fake written from memory tests the memory").

---

## 1. Verdict in one paragraph

The brief's load-bearing insight is correct and, if anything, understated. personalAgent already has the whole "an agent operates the system safely" mechanism: a governed toolbox with a fixed audit→allow-list→policy→decision→run→audit order, permission tiers, argument-bound single-use confirmations, structural exclusion of send/approve/ATS tools, an events-table audit sink on the run-id spine, and three renderers (CLI, HTTP, Telegram) of one confirmation mechanism. The MCP server is the **fourth renderer**, and the SDK's confirmation primitive (elicitation via a *resolver*) maps onto the existing `ask` hook almost one-to-one. **The true gap is not access — it is coverage:** the assistant's tool surface was designed for a chat assistant that *answers*, so it has no governed way to *pull jobs, re-run matching, fit-check, draft, or move an application through its lifecycle*. Those actions exist only as API routes and scripts, with their orchestration logic duplicated in each. Building the MCP is mostly (a) adding those operator actions as governed tools, (b) extracting two service functions the routes currently inline, and (c) a thin bridge from `Registration` → MCP tool.

---

## 2. What exists (read, not assumed)

### 2.1 The governed core — `src/agentkit/` (domain-agnostic, R30)

| Piece | File | What it gives the MCP |
|---|---|---|
| `GuardedToolBox` | `src/agentkit/guard.py` | `execute()` order is fixed: audit intent → per-turn allow-list → `gate.decide` → (CONFIRM → `_confirm`) → audit decision → run → audit result (size only). Refusals are *results*, not exceptions. `ask: Callable[[name, args, policy], bool] | None` is the confirmation channel; `None` means everything needing approval is refused with "no confirmation channel is available". `allowed: frozenset[str] | None` narrows both `specs()` and `execute()` — a tool that would be refused is never shown. |
| `Gatekeeper` / `PolicyBook` / `ToolPolicy` | `src/agentkit/permissions.py` | READ/ACT/ADMIN × NEVER/SESSION/ALWAYS × `costly`. `request()` mints a server-side nonce bound to `sha256(sorted-json(args))` with a 300 s TTL; `redeem()` is single-use and DENIES on unknown, expired, different tool, or changed arguments. `UNIVERSALLY_EXCLUDED` (execute_sql, run_shell, http_fetch, read_file…) is unioned into every book; `guard()` raises at registration. Undeclared tools are DENIED. |
| `Auditor` / `AuditSink` | `src/agentkit/audit.py` | Three spans per call (`tool_intent`, `tool_decision`, `tool_result`), args redacted to 60-char previews, results as byte counts. A failing sink raises `AuditUnavailable` **before** the policy is consulted (R27). `close()` writes a `run` event with `kind_detail="agent_session"` so the session lands in the host's run ledger. `note()` adds host-defined lines on the same spine. |
| `SessionContext` / `Surface` | `src/agentkit/session.py` | Actor, surface (CLI/WEB/CHAT), run_id, `admin_surfaces`. Carries no transcript by design (R28, asserted by test). `may_confirm_admin()` is how chat is kept from approving config writes. |
| `ToolBox` / `ToolSpec` / `validate_tool_schema` | `src/agentkit/tools.py`, `src/agentkit/llm/types.py` | Tool `parameters` are already portable JSON Schema: object → primitive/enum/array-of-primitive properties, one level of nesting, every property described. **This is a valid MCP `inputSchema` verbatim.** Results are strings capped at `MAX_RESULT_CHARS=4000`; a raising tool becomes an error result. |
| `FtsIndex` / `Trust` / `render()` | `src/agentkit/knowledge.py` | Posting text is `Trust.UNTRUSTED`, nonce-fenced when rendered. Unchanged by the MCP. |

Three tests hold the boundary (`tests/test_agentkit_llm.py`: import check, vocabulary check, no import cycles). Any agentkit change the MCP needs must stay generic.

### 2.2 The domain half — `src/jobagent/assistant/`

- `manifest.py:build_assistant(store, settings, sink, surface, ask, actor, base_url, admin_surfaces={WEB, CLI}, cost_budget=20, search=True)` wires PolicyBook(`EXCLUDED`) + Gatekeeper + Auditor + SessionContext + GuardedToolBox and registers every `Registration` from `build_tools()`. One call is a complete governed session. `SYSTEM_PROMPT` states the boundaries (cannot send/approve; tool text is data; never repeat a credential).
- `tools.py:build_tools(store, settings, links, index)` → 15 `Registration(spec, run, policy)`:

  | Tool | Tier | Confirm | Notes |
  |---|---|---|---|
  | pipeline_health, recent_runs, run_detail, top_matches, search_postings, job_detail, applications, needs_followup, upskill, current_config, propose_config_change, request_human_action | READ | NEVER | Renderers are key-asserted against a *populated* store (R32 test). `current_config` masks secrets. |
  | triage | ACT | SESSION | dismiss / snooze / active, optional note |
  | apply_config_change, rollback_config | ADMIN | ALWAYS | `CONFIG_WRITABLE` allow-list; snapshot before write; refused from `Surface.CHAT` |

  `EXCLUDED` = {approve_and_send, approve_application, apply_to_job, submit_application, send_email, send_message, ats_preview, ats_apply, run_ats, fill_form, set_approved}: **absences**, enforced by `PolicyBook.guard()`, tested by name (`test_no_sending_or_approving_tool_is_registered`) and by AST reachability of `smtplib` from the tool module (`test_the_agent_cannot_reach_a_sender_even_transitively`). `request_human_action` is the escape hatch: it returns a dashboard deep link.
- `config_policy.py`: `CONFIG_WRITABLE` (ingest gate fields + model names), `FROZEN = MANAGED_FIELDS − CONFIG_WRITABLE` (computed complement), `MUST_STAY_FROZEN` (egress + inlet + SMTP), `preview()` dry-runs a proposed gate over real stored rows and **refuses** a filter that drops everything, `Snapshotter` copies `data/secrets.enc` aside before a write.
- `knowledge.py`: postings → UNTRUSTED chunks; `open_index(store)` on the store's own connection.
- `evalset.py`: selection / grounding / in-bounds cases; `scripts/eval_assistant.py --floors`.

### 2.3 The three existing renderers (the pattern the MCP joins)

| Renderer | File | Surface | How it confirms |
|---|---|---|---|
| CLI | `scripts/ask.py` | CLI | Blocks on `input()`; `confirm_at_the_terminal` renders `preview().render()` or `describes` + args, reads y/N. `--read-only` passes `ask=None`. |
| HTTP | `src/jobagent/api/assistant_routes.py` | WEB | Cannot block: `capture()` mints a nonce, stores `(tool, args, digest, card)` in a process-local `PendingRegistry`, returns `False` so the turn completes; `POST /assistant/confirm/{nonce}` (empty body) re-executes with `ask=lambda n,a,p: n==tool and digest matches`. The gatekeeper mints and redeems its own nonce underneath — **binding enforced twice**. |
| Telegram | `src/jobagent/bot/assistant_bridge.py` | CHAT | Same two-phase shape; nonce fits a 64-byte callback; `Surface.CHAT ∉ admin_surfaces` so config writes are refused by the gatekeeper, not by the bot remembering a rule. |

Three things are **triplicated** across them and would become quadruplicated: `EventSink` (→ `store.log_event(Event(kind, payload))`), the card renderer (`_card_for` / `confirm_at_the_terminal` / `_card`: `preview().render()` for `apply_config_change`, else `describes` + `k: v` lines), and the "build assistant → execute confirmed call → `auditor.close()`" sequence. Consolidating these is in scope for the MCP work (the brainstorming skill: improve the code you are working in, nothing unrelated).

### 2.4 The API — `src/jobagent/api/app.py` (845 lines)

Route table (verified by grep): `POST /auth/login`; `GET/PUT /config`, `GET/PUT /profile`, `GET /profile/cv` (all auth); `GET /health` (open always); reads `GET /stats /jobs /applications /job/{id} /inbox/proposals /followups /upskill /analytics /sources /runs /runs/{id}` under `read_auth` (no-op unless `JOBAGENT_REQUIRE_AUTH_READS`); writes `PATCH /applications/{id}`, `POST /triage/{id}`, `/jobs/purge`, `/inbox/proposals/{id}`, `/followups/{id}/draft`, `/match`, `/ingest` (202 + run_id, lock acquired synchronously → 409), `/fit`, `/apply/prepare`, `/apply/{id}/approve`, `/ats/preview`, `/ats/{id}/submit`, `/assistant/ask`, `/assistant/confirm/{nonce}` — every non-GET carries `dependencies=auth` (R19, route-table test).

Two pieces of *process logic live only in routes*:
- **Lifecycle transitions** — `update_application` (`app.py:458-484`) and `decide_proposal` (`app.py:571-617`) each inline `can_transition` → 422 with `allowed_next` → `correction` flag → `status_correction` event → `update_application(status=)`. Two copies today; the MCP would be a third.
- **The ingest pass** — `_ingest_task` (`app.py:175-185`: `run_ingestion(build_adapters, gate=IngestGate.from_settings) → run_matching → release_lock`) and `scripts/pipeline.py` (same plus digest + the `run` summary event) and `scripts/ingest.py` (ingest only, no run_id, no gate). Three callers, three shapes.

### 2.5 The store and service layer the tools would call

- `Store` (`src/jobagent/store/db.py`, 1055 lines) opens `sqlite3.connect(db_path)` with the default `check_same_thread=True` → **a Store must be used on the thread that created it** (R15). Relevant methods: `get_matches(limit, min_score, max_age_days, location, keywords, exclude/include_locations, sources, hide_triaged, offset, max_score, min_salary, last_seen_days, companies, triage_states)` (`:634`, ordered by score, shares `_row_predicates` with `purge_jobs`), `get_job`, `get_match`, `stats`, `pipeline_health`, `list_applications`, `get_application`, `update_application(**fields)` (allow-listed columns incl. `status`, `approved_at`, `submitted_at`), `applications_needing_followup`, `set_triage(job_id, state=_KEEP, snoozed_until=_KEEP, note=_KEEP)` (`:916`, omitted fields keep their value — a note can be written without touching state), `clear_triage`, `try_acquire_lock/release_lock` (`pipeline`, 2 h TTL), `list_runs(limit, kind_detail=None)` (`:992`, filters agent sessions out by default), `events_for_run(run_id)` (`:1022`, flattens payload into each event), `log_event`.
- Service functions: `ingestion.runner.run_ingestion(adapters, store, run_id, gate)`, `ingestion.registry.build_adapters(settings)`, `ingestion.gate.IngestGate.from_settings / resolve_sources / ALL_SOURCES`, `matching.run_matching(store, profile, llm, run_id)`, `fit.assess_fit(job, profile, cv_text, llm) -> FitReport` (heuristic fallback when no LLM), `apply.flow.prepare_application(store, job, profile, cv_master_md, llm, settings) -> AssetBundle` (writes CV variant + application in `awaiting_approval`, sends nothing, logs `prepare`; every generator receives the CV — R1a), `apply.flow.approve_and_send` (the **only** sender — stays unreachable), `bot.service.MatchFilter / ranked_matches(store, n, flt, offset, max_per_company)`, `digest.diversify`, `core.schemas.ALLOWED_TRANSITIONS / allowed_next / can_transition`, `llm_client.build_llm(settings)` (agentkit `LLMService`), `agentkit.llm.chain.build_chain(settings, report=True)` (offline chain report).
- Config: `config.Settings` (pydantic-settings, `.env`, `db_path`, `dashboard_password`, `master_key`, all provider keys/models, ingest gate fields, rate limits), `preferences.load_preferences() -> Preferences(profile, watchlist, sources)` (three-layer merge; `save_overlay(patch)` writes `data/profile.json`; `load_cv_master/save_cv_master`), `secrets_store.SecretStore` (Fernet), `setup_wizard` pure functions (`Answers`, `env_updates`, `profile_overlay`, `merge_env`, `parse_env`, `next_steps`), `scripts/check_profile.py` (template-vs-edited detection; logic is in the script, not importable), `scripts/seed_demo.py:seed(db_path, jobs, seed_value)` with the `[DEMO DATA …]` marker.

### 2.6 Onboarding today

`make setup` (interactive, merges `.env` key-by-key, writes `data/profile.json`), `make demo` (seeds `data/demo.db`), `make check` (preflight warnings), `make install`, `make pipeline`. `AGENTS.md` and `CLAUDE.md` are near-identical orientation docs; neither tells an agent how to *operate* the system. No `.mcp.json`, no `mcp` dependency, no `uv.lock` entry. The public-readiness spec adds `make quickstart` and a scriptable `make onboard`; this work must consume, not duplicate, those.

---

## 3. Protocol and client facts (verified 2026-09-13)

### 3.1 The MCP specification

- **Current revision is `2026-07-28`** and it is a breaking redesign: stateless (no `initialize` handshake; version + capabilities travel in `_meta` on every request), `server/discover` is mandatory, `Mcp-Session-Id` removed, server-initiated requests replaced by **Multi Round-Trip Requests (MRTR)** — a tool returns `resultType: "input_required"` with `inputRequests` (e.g. an `elicitation/create`) and an opaque `requestState`; the client answers by *retrying the original call* with `inputResponses` + `requestState`. Servers **MUST** treat `requestState` as attacker-controlled and integrity-protect it; single-use must be enforced server-side. Roots, Sampling and Logging are deprecated; results carry `ttlMs`/`cacheScope`; `tools/list` **SHOULD** be deterministic (prompt-cache hits).
- **Previous revision `2025-11-25`** ("legacy" in the spec's own terminology) keeps `initialize` and server-initiated `elicitation/create`. A **dual-era** server answers both; the compatibility matrix says a modern-only server *fails* legacy clients.
- Tools: `name`, `title`, `description`, `inputSchema`, optional `outputSchema` + `structuredContent`, `annotations` (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` — clients MUST treat them as untrusted hints), execution errors as `isError: true` results (models can self-correct), protocol errors as JSON-RPC errors. Spec: "there SHOULD always be a human in the loop with the ability to deny tool invocations."
- Resources: URI-identified, `resources/list` / `read` / `templates/list` (RFC 6570), `mimeType`, annotations (`audience`, `priority`, `lastModified`); application-driven, not model-driven. Prompts: user-controlled (slash-command-like), `arguments`, message lists.
- Elicitation: form mode is a **flat object of primitives/enums only**; servers **MUST NOT** request secrets via form mode (URL mode exists for that); clients MUST show which server is asking and allow decline/cancel. The SDK docstring is explicit that "if the client is an agent, it might decide how to handle the elicitation — either by asking the user or automatically generating a response" — i.e. **the server cannot prove a human answered.**
- stdio: newline-delimited JSON-RPC; server MUST NOT write anything but MCP to stdout; stderr is for logs; exit on stdin EOF. Security best practices for local servers: use stdio to limit access to the launching client; if HTTP, require a token and bind locally.

### 3.2 The Python SDK — `mcp` 2.2.0 (PyPI, released 2026-09-07, Python ≥ 3.10)

Introspected, not read from the README (which describes almost none of this):

- High-level class is **`mcp.server.MCPServer`** (`FastMCP` module still present). `MCPServer(name, title, description, instructions, version, lifespan, request_state_security, …)`; `add_tool(fn, name, title, description, annotations: ToolAnnotations, meta, structured_output)`; `@resource(uri_template, mime_type, annotations)`; `@prompt(name, title, description)`; `run(transport="stdio" | "sse" | "streamable-http")`; `run_streamable_http_async(host="127.0.0.1", port, transport_security=…)`. Tool input schemas are derived from the Python signature via pydantic (so `Annotated[str, Field(description=…)]` carries property descriptions).
- **Dual-era by default**: `lowlevel.Server.run` drives `serve_dual_era_loop` — legacy `initialize` clients and modern per-request-`_meta` clients are both served. `LATEST_PROTOCOL_VERSION = "2026-07-28"`, `DEFAULT_NEGOTIATED_VERSION = "2025-03-26"`. `types.InputRequiredResult`, `DiscoverResult`, `InitializeRequest` all present.
- **Confirmation primitive**: `Context.elicit(message, schema)` exists but *only works on legacy connections* (docs: on a 2026-07-28 connection "these calls fail"). The era-neutral form is a **resolver**: a tool parameter `Annotated[T, Resolve(fn)]` is filled by running `fn` before the body; `fn` may return `Elicit(message, schema)`; "the transport follows the negotiated protocol: ≥ 2026-07-28 batches the requests into an `InputRequiredResult` and resumes when the client retries; ≤ 2025-11-25 sends each server-to-client request mid-call." With `Annotated[ElicitationResult[T], Resolve(fn)]` the body receives accept/decline/cancel and branches. `requestState` is sealed by `RequestStateSecurity` (AES-GCM, HKDF; `src: mcp/server/request_state.py`), and "a resolver's own computation always wins over anything the client echoes back."
- `ToolAnnotations(title, read_only_hint, destructive_hint, idempotent_hint, open_world_hint)`; `CallToolResult(content, structured_content, is_error, result_type)`; `ElicitResult(action ∈ accept|decline|cancel, content: flat primitives)`.
- **Testing**: `from mcp import Client; async with Client(server_object, raise_exceptions=True, elicitation_callback=…) as c: await c.call_tool(...)` — in-process, no subprocess, "era-neutral" (`mode="auto"`). This is the `TestClient(create_app())` equivalent (R17).
- `stdio_server()` **re-points fd 1 at stderr while serving**, so a stray `print()` in the import chain cannot corrupt the wire.
- Runtime deps: anyio, httpx2 (coexists with the project's httpx 0.28), jsonschema, mcp-types, opentelemetry-api, pydantic ≥ 2.12 (project has 2.13.4), pyjwt[crypto], python-multipart, sse-starlette, starlette, uvicorn (already present via the `api` extra), typing-extensions. Not in `uv.lock` today.

### 3.3 The clients

- **Claude Code**: project-scope `.mcp.json` at repo root (`{"mcpServers": {"name": {"type": "stdio", "command", "args", "env"}}}`), `${VAR}` / `${VAR:-default}` expansion, **prompts for approval on first use** of a project-scoped server (except `-p` / SDK / cloud sessions — `disabledMcpjsonServers` and `--strict-mcp-config` exist). Tools appear as `mcp__<server>__<tool>`, resources as `@<server>:<uri>`, prompts as `/mcp__<server>__<prompt>`. **Supports elicitation dialogs** (the call blocks until the dialog closes). Tool output over 25 k tokens (`MAX_MCP_OUTPUT_TOKENS`) is spilled to a file; a server can raise a tool's threshold via `_meta["anthropic/maxResultSizeChars"]`.
- **Codex CLI**: `~/.codex/config.toml` or project `.codex/config.toml` ("trusted projects only"), `[mcp_servers.<name>] command / args / env / cwd / startup_timeout_sec (10) / tool_timeout_sec (60)`, `codex mcp add <name> -- <cmd>`. Documented support: **tools and server `instructions` only** — no elicitation, resources, or prompts. Consequence: on Codex every ACT/ADMIN tool is refused ("no confirmation channel"), reads work, and the process guidance must live in `instructions`, not in prompts. The 60 s default tool timeout rules out a blocking ingest pass.

---

## 4. What the MCP reuses verbatim

1. `build_assistant()` — the entire governed session. The MCP passes `surface=Surface.AGENT` (new member), an `ask` hook wired to elicitation, and the same `StoreSink`.
2. `ToolSpec.parameters` → MCP `inputSchema`, property for property. `ToolSpec.description` → tool description. `ToolPolicy` → `ToolAnnotations` (READ → `read_only_hint=True`, `idempotent_hint=True`; ALWAYS-confirm → `destructive_hint=True`; `pull_jobs` → `open_world_hint=True`).
3. `Gatekeeper.request/redeem` — the nonce + digest binding under every confirmation, unchanged (R29).
4. `Auditor` + events table + `list_runs(kind_detail="agent_session")` + `events_for_run` + `GET /runs/{id}` — the debugging story, unchanged (Q6 of the brief).
5. `EXCLUDED` + `UNIVERSALLY_EXCLUDED` + `PolicyBook.guard()` — R25/R26, unchanged and extended with a few more names.
6. `preview()` / `Impact.render()` / `Snapshotter` — the config-change card and rollback.
7. Every service function in §2.5. No new access path to the store; the MCP never calls the REST API (the tools.py docstring gives the reason: a URL can be redirected by a string the model emits; a named Python function cannot).
8. Test doubles: `FakeLLM`, `tmp_path` stores, `ListSink`, `Settings()` with isolated paths.

---

## 5. The true gap

### 5.1 Missing operator capabilities (absences in the current toolbox)

| Brief §1.2 requirement | Today | Gap |
|---|---|---|
| Pull jobs | `POST /ingest`, `scripts/pipeline.py` | No governed tool; orchestration inlined in `_ingest_task` and two scripts |
| Run / refresh matching | `POST /match` | No tool |
| Read and **sort** ranked matches | `top_matches` (min_score only, score order, hide_triaged fixed) | No filter/sort/pagination surface; `get_matches` has 15 filters the tool never exposes; no structured output |
| Triage | `triage` ✅ | Note-only annotation (research findings) needs `set_triage(note=)` without a state change |
| Track applications | `applications`, `needs_followup` (read) | No status-transition tool; the R23 logic is inlined in two routes |
| Fit / draft | `POST /fit`, `POST /apply/prepare` | No tools; `prepare_application` is R2-safe (sends nothing) but was never exposed to the assistant |
| Read store / settings / preferences / history | `current_config`, `job_detail`, `run_detail` ✅ | No profile/preferences read; no lifecycle-graph read; no setup/first-run status |
| Research a role / company | `job_detail`, `search_postings` | No company-level aggregation; web research is R25-forbidden server-side — must be the *client's* tools + a place to record findings |
| One-command onboarding | `make setup` (interactive) | Scriptable `make onboard` is the companion spec's; the MCP needs a read-only "what is missing" tool and a committed client config |
| Record every decision | Auditor ✅ | A session that dies without `close()` leaves no `run` row; needs an opening note |

### 5.2 Missing infrastructure

- `src/jobagent/mcp/` does not exist; no `mcp` extra; no `.mcp.json`; no `make mcp*` targets; AGENTS.md/CLAUDE.md say nothing about operating the system as an agent.
- No `Surface` for "coding agent" (CLI/WEB/CHAT only).
- `ToolResult` is text-only; a coding agent sorting 200 matches wants JSON (`structuredContent`).
- Structural duplication that the fourth renderer would worsen: `EventSink` ×3, card rendering ×3, transition logic ×2, ingest-pass orchestration ×3.

### 5.3 Constraints discovered that shape the design

1. **Threading.** The SDK runs sync tool functions in worker threads; `Store` is `check_same_thread=True`; `Gatekeeper._pending/granted` and `Auditor` counters are plain dicts. Either every governed call is serialized onto one owning thread, or each call opens its own Store *and* the gate/auditor are locked. (Design: one owning thread.)
2. **Human-ness of a confirmation.** The server cannot distinguish a Claude Code dialog answered by a person from a client that auto-accepts. ADMIN (config rewrite) therefore should not be confirmable from the agent surface by default — the same call this project already made for Telegram.
3. **Tool timeouts.** Codex defaults to 60 s per tool; an ingest pass takes minutes. `pull_jobs` must return a `run_id` and let the agent poll, exactly as `POST /ingest` does.
4. **Token cost of the tool list.** memory.md measured 1,047 tokens of tool schemas per Baer turn. Adding operator tools to the *shared* toolbox would tax every Telegram `/ask`; the new tools should be visible per surface (`GuardedToolBox.allowed` / a `surfaces` field on `Registration`).
5. **stdout discipline.** Several modules `print()` (scripts, `llm_doctor`); the SDK diverts fd 1 while serving, but `--check` / self-test modes must not serve *and* print on the same descriptor.
6. **Elicitation cannot carry secrets** (spec MUST NOT) — and this project already forbids the agent from handling credentials (R9, `check_writable` refuses `SECRET_FIELDS`). Onboarding via MCP is therefore *status + instructions + non-secret preference writes*, never `.env` writes.
7. **Deterministic `tools/list`.** Registration order must be stable (list, not set) — cheap, and it matters for the client's prompt cache.

---

## 6. Answers the investigation supports (resolved in the design spec)

| Brief §6 | Finding |
|---|---|
| 1 Transport | stdio. Both target clients launch stdio servers from project config; the SDK is dual-era; Streamable HTTP exists in the SDK (`127.0.0.1`, `TransportSecuritySettings`) for a later "running server" mode. |
| 2 Tools vs resources | Every action and every argument-taking read is a **tool** (Codex sees only tools). **Resources** are JSON views of READ tools (status, profile, settings, lifecycle, matches, job, applications, runs) served *through* `execute()` so they are audited. **Prompts** `onboard`/`operate` + server `instructions` carry the process. |
| 3 Auth | stdio runs as the operator, in the repo, on the same `Settings` as `scripts/ask.py`; no bearer token is ever held by the agent (the MCP calls the service layer in-process). Authority = permission tiers + surface. R19 untouched. |
| 4 Process fidelity | `ALLOWED_TRANSITIONS` enforced by a shared `lifecycle.transition()`; corrections are a separate ALWAYS-confirm tool; drafting ends at `request_human_action`; there is no send tool; `instructions` and the `operate` prompt state the loop. |
| 5 Research | Composition: `company_dossier` + `job_detail` + `search_postings` over the store; the client's own web tools do the web; `annotate_job` records findings. No `http_fetch` (R25). |
| 6 Debugging | One Auditor per server process on the run-id spine; opening note + closing `run`; `runs`/`runs/{id}` resources; `GET /runs/{id}` and `/assistant/sessions` unchanged. |
