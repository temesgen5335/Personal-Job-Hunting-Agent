# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html) as
scoped in [docs/VERSIONING.md](docs/VERSIONING.md) — which is worth reading, because
"public API" means something specific for a self-hosted single-user agent.

## [Unreleased]

Planned work is tracked in [docs/ROADMAP.md](docs/ROADMAP.md), grouped by the release
that will carry it.

## [3.4.0] — 2026-08-18

*Theme: the deployment the docs teach is no longer the one that leaks.*

### Added
- **`JOBAGENT_REQUIRE_AUTH_READS`** — opt-in authentication on GET routes. Off by
  default, which stays correct on the `127.0.0.1` bind; turn it on for any deployment
  the network can reach. `/health` stays open even then, because it is a liveness probe
  (the Docker `HEALTHCHECK` calls it) and reports nothing about the job search.
  A route-table test asserts every other GET is gated, so one added next year without
  `dependencies=read_auth` fails in CI rather than leaking quietly.
- **`scripts/api_token.py`** and `JOBAGENT_API_TOKEN` — the dashboard renders reads
  server-side and has no browser session to borrow, so it carries a derived token.
- **Per-client rate limits** on assistant/LLM calls (60/h), ingestion (20/h) and writes
  (600/h), returning `429` with `Retry-After` and naming the env var that raises it.
  Reads are deliberately unlimited: the dashboard makes several per page load, and a
  limiter that throttles normal use is one that gets switched off.
- `JOBAGENT_MAX_PURGE_ROWS` as a safety valve, defaulting to unlimited — the purge UI
  already shows an exact count and requires a second click, so consent is obtained
  before the delete and a cap would only add friction.
- Exposure warnings at the top of `docs/DEPLOYMENT.md` and
  `docs/DEPLOYMENT_ALTERNATIVES.md`, where the split-deploy path is taught.

### Changed
- The API **refuses to start** if read auth is on without `DASHBOARD_PASSWORD`. No token
  would exist, so every page would 403 forever — which reads as a broken app rather than
  a missing setting.
- The dashboard distinguishes "the API is up and refusing me" from "the API is down".

## [3.3.0] — 2026-08-18

*Theme: fifteen minutes from clone to first ranked job, without editing TOML by hand.*

### Added
- **`make setup`** — an interactive first-run wizard. Writes `.env` and your profile
  overlay, generates `JOBAGENT_MASTER_KEY` and (if you want) a dashboard password.
  Safe to re-run: `.env` is merged key-by-key, so comments and anything you tuned by
  hand survive, and unanswered prompts never overwrite an existing value.
  Logic lives in `jobagent.setup_wizard` as pure functions; only `scripts/setup.py`
  touches stdin, so it is tested without driving a terminal.
- **`make demo`** — seeds `data/demo.db` with 40 fictional postings, strong matches, a
  triaged row and three applications, so every dashboard page has something to render
  before you commit any credentials. It refuses to write into a store that already has
  jobs, and every seeded posting is marked as demo data in its description.
- **Containers** — `Dockerfile` (API, bot, CLI), `dashboard/Dockerfile` (Astro SSR),
  `compose.yml`, and `make docker_up` / `make docker_down`. Host ports bind to
  `127.0.0.1` only, because GET routes are unauthenticated. Playwright is opt-in via
  `--build-arg WITH_BROWSER=1` rather than a ~400 MB default.
- README now opens with a five-command fast path and a Docker section.

### Fixed
- The `make test` target described a "99-test offline suite".

## [3.2.0] — 2026-08-18

*Theme: a stranger can legally use this, and gets their own job search rather than mine.*

### Added
- **`LICENSE`** (MIT). `pyproject.toml` had claimed MIT for months with no license file,
  which legally means all rights reserved — nobody could use the code.
- **`config/preferences.example.toml`** — a neutral, commented template. The loader falls
  back to it, so a fresh clone runs before it is edited.
- **`scripts/check_profile.py`**, wired into `make check`: warns while the search profile
  is still template values, naming the fields.
- `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, issue forms, and a PR template.
- `uv.lock` and `dashboard/package-lock.json` are now committed.
- `tests/test_packaging.py` — nine invariants covering every finding above.

### Changed
- **`config/preferences.toml` is no longer committed.** It carried a real search profile:
  location, timezone, 9 target roles, 37 core skills, 26 tuned skill weights and a
  40-company watchlist. Identity had been scrubbed to placeholders long ago, but that
  work framed "personal" as contact details — a PII scrub is not a personalization scrub.
- README truth-pass: profile setup now describes the Settings UI (the real path since
  3.0.0), the CV lives at `data/cv_master.md`, and the "nothing is hard-coded to one
  person" claim is replaced with one that is true.
- `.gitignore` uses case-class patterns (`*[Cc][Vv]*.pdf`, `[Rr]esume`) — matching is
  case-sensitive on Linux, which macOS hides.

### Fixed
- **The maintainer's real name was still in tracked test fixtures**, along with their
  actual CV filename, and a timezone example named their city. Replaced with neutral
  values; `tests/test_packaging.py` now fails on maintainer identity in any tracked file
  outside the authorship ones (a licence and a security contact must name a person).
- Removed a personal URL from a dashboard CSS comment.

### Upgrade

None required. Your existing `config/preferences.toml` is untouched on disk; it is simply
no longer tracked by git.

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

[Unreleased]: https://github.com/temesgen5335/personalAgent/compare/v3.4.0...HEAD
[3.4.0]: https://github.com/temesgen5335/personalAgent/compare/v3.3.0...v3.4.0
[3.3.0]: https://github.com/temesgen5335/personalAgent/compare/v3.2.0...v3.3.0
[3.2.0]: https://github.com/temesgen5335/personalAgent/compare/v3.1.0...v3.2.0
[3.1.0]: https://github.com/temesgen5335/personalAgent/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/temesgen5335/personalAgent/compare/v1.0.0...v3.0.0
[2.4.0]: https://github.com/temesgen5335/personalAgent/releases/tag/v2.4.0
[2.3.0]: https://github.com/temesgen5335/personalAgent/releases/tag/v2.3.0
[2.2.0]: https://github.com/temesgen5335/personalAgent/releases/tag/v2.2.0
[2.1.0]: https://github.com/temesgen5335/personalAgent/releases/tag/v2.1.0
[2.0.0]: https://github.com/temesgen5335/personalAgent/releases/tag/v2.0.0
[1.0.0]: https://github.com/temesgen5335/personalAgent/releases/tag/v1.0.0
