# Versioning & Release Policy

## One number, one place

`src/jobagent/__init__.py` holds `__version__`. Everything else derives from it:

| Surface | How it gets the version |
|---|---|
| `pyproject.toml` | `dynamic = ["version"]` + hatchling reading the attribute |
| FastAPI app + `GET /health` | imports `jobagent.__version__` |
| Dashboard sidebar | reads `/health` — never its own copy |
| Git tag | `v{__version__}`, created at release |

**Never add a second literal.** The API once reported `2.0` while `pyproject.toml` said
`3.0.0`; two numbers both claiming to be the version, with nothing to say which was
wrong. `tests/test_versioning.py` now fails on any hardcoded version string under
`src/`, on a `version =` literal in `pyproject.toml`, on installed metadata that
disagrees with the code, and on a release with no CHANGELOG entry.

After bumping, reinstall so the metadata refreshes:

```bash
uv pip install -e .          # or the test above will tell you it is stale
```

## What SemVer means *here*

This is a self-hosted, single-user application, not a library. Almost nobody imports
`jobagent` as a package, so the usual "public API" definition would make every release
a MAJOR. The contract that actually matters to a user is **their data and their
configuration**, so that is what versioning protects:

**MAJOR** — you must do something before upgrading.
- A store migration that is not automatic, or that cannot be rolled back
- A config key removed or given new meaning (`.env`, `preferences.toml`, `data/profile.json`)
- A REST route removed or its response shape changed incompatibly
- A default that changes what the agent *does* (e.g. an apply path that stops asking)

**MINOR** — new capability, nothing to do.
- New features, routes, adapters, dashboard pages, assistant tools
- New config keys with safe defaults
- Store schema additions that migrate themselves on open

**PATCH** — fixes only.
- Bug fixes, performance, docs, tests, copy changes
- No new config keys, no schema change, no route change

### The rules that outrank SemVer

Two categories are never a MINOR or PATCH, whatever the diff looks like:

1. **Anything touching the HITL gate (R2) or CV fabrication (R1)** is a MAJOR, even if
   the change is a "fix". These are the safety properties a user trusts the system for;
   a silent change to either is the one thing that must never arrive unannounced.
2. **Anything that widens what is reachable without authentication** is a MAJOR. A route
   moving from gated to open changes the deployment's threat model.

## Release checklist

1. `make test` — the suite must be green, including `tests/test_docs.py`
2. Bump `__version__`; `uv pip install -e .`
3. Move `## [Unreleased]` items into a dated `## [X.Y.Z]` section in `CHANGELOG.md`
4. Update any count claimed in `README.md` / `.claude/context.md` (tests, features)
5. Commit, then `git tag -a vX.Y.Z -m "..."` and push tags
6. If the release is MAJOR, the CHANGELOG entry must open with an **Upgrade** subsection
   saying exactly what the operator has to do

## Pre-release and deployment

There is no staging environment: the operator *is* the deployment. A change that wants
real-world exposure before a tag ships as `X.Y.Z-rc.1`, which SemVer orders before
`X.Y.Z`. Use it for anything that touches ingestion volume, LLM spend, or the store.
