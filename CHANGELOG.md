# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) as
scoped in [docs/VERSIONING.md](docs/VERSIONING.md) — which is worth reading, because
"public API" means something specific for a self-hosted single-user agent.

## [Unreleased]

Planned work is tracked in [docs/ROADMAP.md](docs/ROADMAP.md), grouped by the release
that will carry it.

## [3.1.0] — 2026-08-18

### Added
- **Pull Jobs** — a button on the Overview that runs a full ingest + match pass on
  demand (`POST /ingest`), polling the run ledger for per-source progress. Until this,
  nothing in any interface could start a pass.
- **Filtered job cleanup** — `POST /jobs/purge` and a preview-then-confirm panel on the
  Jobs page. Presets for weak matches, dismissed jobs, stale postings, or everything
  matching the current filters. Applications, tailored CVs and triage notes are spared
  unconditionally.
- **Shared sign-in prompt** — `window.JA.signIn()`: any page's `401` raises a password
  modal and retries once, writing the same session token the Settings page uses.
- **Version reporting** — `/health` returns the running version; the dashboard sidebar
  shows it.

### Fixed
- **The triage queue under-reported by 5×.** `/jobs` reused the bot's shortlist builder,
  whose per-company cap is correct for a digest and wrong for a browsable queue: 231
  strong untriaged matches rendered as 46. Selection now runs through one shared
  predicate builder, so the count and the rows cannot diverge.
- `/jobs` defaulted to a 7-day window while the queue badge had no date filter, so a
  stale pipeline showed nothing at all. The default is now "any date".
- `/jobs` shipped the full posting `description` on every row — 95% of the payload, for
  text the list never renders. Responses dropped from 3.0 MB to 346 KB.

### Changed
- Dashboard dev port moved from 4321 to **1234** (Makefile, Astro config, CORS default,
  `.env.example`).
- Version is now read from a single literal in `src/jobagent/__init__.py`; `pyproject.toml`
  derives it. The FastAPI app previously reported `2.0` while the package said `3.0.0`.

## [3.0.0] — 2026-08-17

### Added
- **The agent harness.** `src/agentkit/` — a domain-agnostic, capability-aware multi-LLM
  layer: a ranked plan queue that doubles as the failover queue, nine degradation
  strategies, a circuit breaker, and a governed tool seam with no ungoverned path.
- **Baer, the assistant.** 14 in-process tools, permission tiers, argument-bound
  single-use confirmations, FTS5 retrieval over postings fenced as untrusted, and a
  fail-closed audit trail. Reachable from the CLI, the dashboard (page and floating
  bubble), and Telegram.
- **UI-editable profile.** Identity, CV, search preferences, sources and the ATS
  watchlist all persist to a gitignored `data/` overlay through a tabbed Settings page.
- Assistant eval harness with scored floors, and `llm_doctor` for offline diagnosis.

### Security
- Every non-GET API route requires a bearer token and fails closed without
  `DASHBOARD_PASSWORD`.

## [2.4.0] — 2026-07

### Added
- Job detail pages, on-demand fit checks, inline charts, location filters, pagination.

## [2.3.0] — 2026-07

### Added
- Application tracker and analytics: funnel, outcome rates, 30-day timeline.

## [2.2.0] — 2026-07

### Added
- Fit-checker — a confidence score with an explainable report.

## [2.1.0] — 2026-07

### Added
- Encrypted config UI (Fernet secret store), auth-gated settings API, custom
  OpenAI-compatible LLM provider.

## [2.0.0] — 2026-07

### Changed
- **FastAPI orchestrator became the sole backend.** The dashboard now calls a REST API
  instead of reading SQLite directly.

## [1.0.0] — 2026-06

### Added
- First working system: ingestion from six sources, heuristic + LLM matching, the
  Telegram bot, Tier-1 email applications, Tier-2 ATS form-fill, and VPS deployment
  units.

[Unreleased]: https://github.com/temesgen5335/personalAgent/compare/v3.1.0...HEAD
[3.1.0]: https://github.com/temesgen5335/personalAgent/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/temesgen5335/personalAgent/compare/v1.0.0...v3.0.0
[2.4.0]: https://github.com/temesgen5335/personalAgent/releases/tag/v2.4.0
[2.3.0]: https://github.com/temesgen5335/personalAgent/releases/tag/v2.3.0
[2.2.0]: https://github.com/temesgen5335/personalAgent/releases/tag/v2.2.0
[2.1.0]: https://github.com/temesgen5335/personalAgent/releases/tag/v2.1.0
[2.0.0]: https://github.com/temesgen5335/personalAgent/releases/tag/v2.0.0
[1.0.0]: https://github.com/temesgen5335/personalAgent/releases/tag/v1.0.0
