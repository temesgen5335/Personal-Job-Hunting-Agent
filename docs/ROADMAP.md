# Roadmap

Every shortcoming found in the 2026-08-18 open-source review, plus the features that
would make this useful to someone who is not its author — each assigned to a release so
it ships and can be upgraded to deliberately. Version semantics: [VERSIONING.md](VERSIONING.md).

**How to read this.** Items are ordered by *what blocks the next user*, not by what is
most interesting to build. A stranger cannot legally use the code today (no LICENSE) and
would get the author's job search if they did (`preferences.toml`). Those come first even
though an Indeed adapter is the more exciting change.

Status legend: 🔴 blocking · 🟠 high · 🟡 medium · 🟢 nice-to-have

---

## v3.2.0 — "Cloneable" ✅ SHIPPED 2026-08-18

*The theme: a stranger can legally use it, and gets their own job search rather than
someone else's.* Nothing here is hard; all of it is blocking.

### 🔴 1. There is no LICENSE file

`pyproject.toml` declares `license = { text = "MIT" }`. No `LICENSE` exists. Without the
file, default copyright applies — **all rights reserved** — so nobody may legally fork,
modify, or run it, whatever the README implies.

**Fix:** add `LICENSE` with the MIT text and the author's copyright line. Add a
`tests/test_docs.py` case asserting the file exists and that its named license matches
`pyproject.toml`, so the two can never disagree again.

**Value:** the difference between "open source" and "source visible". Ten minutes.

### 🔴 2. `config/preferences.toml` is the author's job search, not a template

Identity fields are placeholders, but everything that drives matching is personal:
`location = "Ethiopia (Addis Ababa)"`, `timezone = "EAT / UTC+3"`, 9 target roles,
37 core skills, 26 tuned skill weights, 8 domains, and a 40-company ATS watchlist. A
backend engineer in Berlin clones this and silently gets an AI-engineer search over
someone else's shortlist — every score wrong, with nothing to say so.

The README's claim *"Nothing is hard-coded to one person — all identity lives in config"*
is true only of contact details.

**Fix:**
- Ship `config/preferences.example.toml` — neutral, commented, deliberately generic
  (`location = "Your City, Country"`, a handful of illustrative roles/skills, an empty
  watchlist with three examples).
- Gitignore `config/preferences.toml` and have `load_preferences()` fall back to the
  example with a loud warning if it is missing.
- `make check` fails while any placeholder is unedited, naming the file and the key.
- Correct the README claim to what is actually true.

**Value:** removes the single biggest "this doesn't work" experience. Every match score
a new user sees is wrong until this is fixed, and nothing currently tells them why.

### 🔴 3. No lockfiles — installs are not reproducible

No `uv.lock`, no `requirements.txt`, and `dashboard/package-lock.json` is *explicitly
gitignored*. Both dependency trees resolve to whatever is newest at install time, so a
fork that works today may not build next month, and a bug report cannot be reproduced.

**Fix:** commit `uv.lock`; remove `dashboard/package-lock.json` from `.gitignore` and
commit it; pin the CI install to the lockfiles.

**Value:** reproducible builds and actionable bug reports. This is table stakes.

### 🟠 4. The README is a release behind

Claimed 515 tests (now 576, fixed in 3.1.0). Tells you to put your CV in
`config/cv_master.md`; it is `data/cv_master.md`. Describes file-based profile editing as
the only path, though Settings has edited everything since 3.0.0. `tests/test_docs.py`
validates cited paths and rule ids but not counts or prose.

**Fix:** a truth-pass over `README.md`, plus a doc test asserting any number the README
claims about the suite matches the collected count.

**Value:** the README is the only thing most visitors read. Stale instructions read as
an abandoned project.

### 🟠 5. Missing community and safety scaffolding

No `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, issue templates, or PR
template. `SECURITY.md` matters more than usual here: the software holds an operator's
CV, contact details, SMTP credentials and API keys.

**Fix:** add all five. `CONTRIBUTING.md` should point at `.claude/` — the governance
docs are unusually strong and are the project's best onboarding asset. `SECURITY.md`
must state the threat model plainly: single-user, secrets in `.env` or the Fernet store,
reads unauthenticated by default, and a private disclosure address.

**Value:** turns a personal repo into one a stranger can contribute to.

### 🟡 6. `.gitignore` protects the CV narrowly

`docs/*CV*.pdf` and `docs/*cv*.pdf` cover exactly two spellings in exactly one directory.
`Cv`, `resume.pdf`, a `.docx`, or a CV anywhere else is not ignored. The git history is
clean today — this is about the next accident.

**Fix:** broaden to `*[Cc][Vv]*.pdf`, `*[Rr]esume*`, `*.docx`, and add a pre-commit hook
(or a CI grep) that fails on a staged file matching those patterns.

---

## v3.3.0 — "First run" ✅ SHIPPED 2026-08-18

*The theme: fifteen minutes from clone to first ranked job, without editing TOML by hand.*

### ✅ 7. Onboarding is chicken-and-egg

Settings can edit the whole profile — but `DASHBOARD_PASSWORD` must already be in `.env`
before any write works, and the API must already be running. So the "no file editing"
story only begins *after* manual file editing. There is no guided setup.

**Fix:** `make setup` — an interactive script that writes `.env` (generating
`JOBAGENT_MASTER_KEY` and prompting for a dashboard password), creates the profile
overlay from answers to a handful of questions (roles, skills, locations, remote?), and
ends by printing the next command. It should be re-runnable and never overwrite without
asking.

**Value:** the largest single drop-off point. Everything after it already works.

### ✅ 8. No container

No `Dockerfile`, no `docker-compose.yml`. For a self-hosted tool that is the first thing
most people look for, and it also solves "Playwright needs a real browser", which is
currently a manual step with an ARM footnote.

**Fix:** a multi-stage `Dockerfile` (API + dashboard) and a `compose.yml` running API,
dashboard and bot with a mounted `data/` volume. Publish to GHCR from a release tag.

**Value:** turns a five-prerequisite setup into `docker compose up`.

### ✅ 9. Nothing to look at before the first ingest

A fresh clone has an empty store, so every dashboard page renders empty states. The
system looks broken before it looks useful.

**Fix:** `make demo` — seed a few dozen realistic postings and a couple of applications
into a throwaway store, clearly labelled as demo data, so the UI can be evaluated in
thirty seconds.

**Value:** lets someone judge the project before committing credentials to it.

---

## v3.4.0 — "Safe to expose"

### 🟠 10. Reads are unauthenticated, and the docs never say so

`/jobs`, `/stats`, `/analytics`, `/applications`, `/followups`, `/runs` and
`/job/{id}` are open. `/applications` and `/followups` reveal **where the operator
applied, what was rejected, and where they are interviewing** — sensitive career data.

Mitigated today by binding `127.0.0.1`. But `PUBLIC_JOBAGENT_API_URL` exists precisely
for split deploys (dashboard on Vercel, API on a VPS), and in that documented
configuration the application history is world-readable. `docs/DEPLOYMENT.md` does not
mention it.

**Fix (3.4.0, opt-in):** `JOBAGENT_REQUIRE_AUTH_READS=true` gating every read behind the
same bearer token, with the dashboard's SSR layer holding a server-side token. Add a
prominent warning to `DEPLOYMENT.md` and `DEPLOYMENT_ALTERNATIVES.md`.

**Fix (4.0.0, default):** flip the default to gated. That changes the deployment threat
model, so by this project's own policy it is a MAJOR.

**Value:** the current default is safe on a laptop and unsafe in the exact configuration
the docs teach.

### 🟡 11. No rate limiting or request bounds

Nothing bounds `/assistant/ask` (LLM spend), `/ingest` (outbound fetching), or purge
size. On a private box this is theoretical; the moment anything is exposed it is not.

**Fix:** a small in-process token bucket per route class, plus a configurable ceiling on
purge size and assistant turns per hour.

---

## v3.5.0 — "More jobs, better matched"

### 🟠 12. No LinkedIn / Indeed / Glassdoor

The single biggest functional gap. `Sources.aggregator` and `Source.aggregator` exist;
the adapter does not. These are where most postings actually are, and all three are
aggressively anti-bot with no public API — so scraping them directly is both fragile and
against their terms.

**Best solution:** a third-party aggregator API rather than a scraper — **JSearch**
(RapidAPI) as the primary, which indexes LinkedIn/Indeed/Glassdoor/ZipRecruiter behind
one endpoint and has a usable free tier; **SerpApi** as an optional second. This keeps
R7 (prefer APIs over scraping) intact, keeps the anti-bot problem someone else's, and
needs no new architecture: `BaseAdapter` + a `Source` value + a registry entry, exactly
as documented in `.claude/agent.md`.

**Value:** likely a 3–10× increase in relevant postings. Everything downstream —
matching, triage, the queue, apply — already scales to it. This is the highest
value-per-line change on the roadmap.

**Watch out for:** aggregator results are lower quality and duplicate the direct
adapters heavily, so ship it *with* item 13.

### 🟠 13. Cross-board deduplication is weak

The dedup hash is `company + title + location`, and the Telegram parser never sets a
company — so those postings dedup on the title line alone. Adding an aggregator makes
this acute: the same role will arrive from Greenhouse, LinkedIn and Indeed with three
different titles.

**Best solution:** two layers. (a) Normalize before hashing — casefold, strip seniority
decorations and location suffixes, canonicalize company via a slug map. (b) A similarity
pass over same-company postings within a time window (token-set ratio over the title,
plus URL-host match), collapsing near-duplicates into one row that remembers every
source it appeared on.

**Value:** removes the most visible quality problem in the queue, and "seen on 3 boards"
is itself a useful ranking signal.

### 🟡 14. Matching is keyword-only, and scores have no provenance

`heuristic_score()` is word-boundary keyword matching. It cannot tell that "LLM
orchestration" and "agentic systems" are the same thing, so a genuinely good posting that
uses different vocabulary scores zero. Separately, heuristic scores **overwrite** LLM
scores each run and nothing records which produced a number.

**Best solution:** add a local embedding pass (`sentence-transformers`, ~80 MB, CPU, no
API cost) as a third signal blended with the keyword and role components — not a
replacement, because the keyword scorer is explainable and the eval harness is built
around it. Store `score_source` and keep the LLM score in its own column so a rerank is
never clobbered.

**Value:** catches the vocabulary-mismatch misses, which are invisible today precisely
because they score zero and never appear. The existing labeled eval set
(`matching/evalset.py`, P@5 floor) makes this measurable rather than a guess.

### 🟡 15. No salary signal

`salary_text` is stored but never parsed, filtered, or ranked on. For most people
compensation is a top-three filter.

**Best solution:** parse to `(min, max, currency, period)` at ingest with a tolerant
regex, store as columns, and add a filter to the ingest gate, the Jobs page and the bot
menu. Leave unparseable text alone rather than guessing.

---

## v3.6.0 — "Close the loop"

### 🟠 16. Application outcomes are entered by hand

The tracker knows what you sent. It learns nothing about what came back, so the funnel
and response rates only reflect what the operator remembered to type in.

**Best solution:** an optional IMAP reader that watches the applying mailbox and
proposes status transitions — a rejection phrase or an interview invitation maps to a
suggested `rejected` / `interview` move, surfaced for **one-tap confirmation**, never
applied automatically. This fits the existing HITL posture exactly (R2's spirit) and the
lifecycle already enforces legal transitions.

**Value:** turns the analytics from a diary into data. Once outcomes are real, the
matching eval can be scored against *what actually got replies* — the single most
valuable feedback signal the system could have.

### 🟡 17. The Telegram handlers have no runtime coverage

`tests/test_bot.py` covers only pure helpers; the handlers in `bot/app.py` need real
`Update`/`Context` objects. This is how a call to an undefined `_llm()` shipped and
crashed `/apply`. A static check now guards that specific class, but it is not coverage.

**Best solution:** a small fake `Update`/`Context` harness (the `FakePage` pattern
already used for Playwright), plus one end-to-end test per command.

**Value:** the bot is the primary interface and the least tested part of the system.

### 🟡 18. No LLM cost visibility

One assistant turn sends ~1,258 tokens before any tool result, 1,047 of them tool
schemas resent every turn — measured, and the reason free tiers drain. Nothing surfaces
spend.

**Best solution:** record tokens and estimated cost per call on the run ledger (the spine
already exists), show a 30-day chart on the Overview, and trim the tool set per turn via
`GuardedToolBox.allowed`, which was built for exactly this.

---

## v4.0.0 — breaking

- **Reads gated by default** (item 10). Threat-model change ⇒ MAJOR.
- **Multiple search profiles.** One person often runs two searches ("senior backend" and
  "AI engineer") with different weights and watchlists. Today profile is a singleton;
  making it a keyed collection changes the config shape.
- **Postgres option.** SQLite is right for one user, but a shared or hosted deployment
  wants concurrent writes. The `Store` interface is already the seam.

---

## Deliberately not planned

- **Auto-submitting applications.** R2. The HITL gate is the product's ethical spine.
- **CAPTCHA solving.** R3.
- **Auto-sending follow-up nudges.** R24 — drafts only, no send path, by design.
- **A general-purpose assistant tool** (`execute_sql`, `run_shell`, filesystem, HTTP
  fetch) or a `delete_jobs` tool. R25/R26 — excluded by absence, not by gate.
- **Scraping LinkedIn/Indeed directly.** R7 and their terms; item 12 is the sanctioned
  route.
