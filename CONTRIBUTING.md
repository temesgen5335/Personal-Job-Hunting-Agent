# Contributing

Thanks for looking. This is a personal-scale project with unusually heavy documentation —
that is deliberate, and it is the fastest way in.

## Read these first

`.claude/` is the project's real design record, written for both humans and AI agents:

| File | What it answers |
|---|---|
| [`.claude/context.md`](.claude/context.md) | What exists, what does not, where the gaps are |
| [`.claude/rules.md`](.claude/rules.md) | The binding constraints, R1–R32 |
| [`.claude/agent.md`](.claude/agent.md) | Architecture, module map, and a table of pitfalls already hit |
| [`.claude/memory.md`](.claude/memory.md) | *Why* the design is the way it is, and what each lesson cost |

The pitfalls table in `agent.md` and the whole of `memory.md` exist so the same bug is
not found twice. Skimming them will save you more time than reading the code.

## Setup

```bash
git clone https://github.com/temesgen5335/personalAgent && cd personalAgent
uv venv && uv pip install -e ".[dev,api,llm,telegram]"
cp config/preferences.example.toml config/preferences.toml   # then make it yours
cp .env.example .env                                         # optional for tests
make test                                                    # no credentials needed
```

The whole suite runs offline. If a test of yours needs the network, a credential, or a
browser, it is the wrong test (R17).

## Ground rules

**The non-negotiables** are in `.claude/rules.md`. The ones that most often surprise
contributors:

- **R2 — never submit an application without explicit approval.** The HITL gate is the
  product's ethical spine, not a feature to optimise away.
- **R1 — never fabricate CV content.** Tailoring reframes real experience only.
- **R26 — exclusion over gating.** A capability the agent must never have is not
  registered at all. Do not add a dangerous tool and guard it.
- **R30 — `agentkit` may never import `jobagent`.** The harness is domain-agnostic;
  three tests hold the line.
- **R32 — read the store keys, do not remember them.** Rendering `None` into
  model-visible text is how a model states a wrong fact confidently.

**House style:**

- Comments explain *why*, not *what*. If a line needs a comment to say what it does,
  rewrite the line.
- Match the surrounding code's density and idiom.
- Pydantic v2 `ConfigDict` only — `class Config:` is banned (R10).

## Tests

Every behaviour change needs a test, and the test must **fail before your fix**. Break
the property, watch it go red, restore, watch it go green. A test that passes with its
own property removed is observing nothing — this project has shipped two of those and
both are written up in `memory.md`.

Prefer asserting the *consequence* over the *sequence*. "Intent is audited before the
policy runs" checked event ordering and survived moving the call; what actually pins it
is that a dead audit sink stops the call before the gatekeeper is asked anything.

## Pull requests

1. `make test` green, including `tests/test_docs.py` — it fails on stale doc claims.
2. Update the docs in the *same* change. When docs and code disagree, the code wins and
   the doc is a bug.
3. Add a `## [Unreleased]` entry to [`CHANGELOG.md`](CHANGELOG.md).
4. Describe *why*, and what you verified. "Tests pass" is not verification for anything
   touching an LLM, a live source, or a browser — those need a real run, and the
   project's history is mostly bugs that a green suite could not see.

## Good first contributions

[`docs/ROADMAP.md`](docs/ROADMAP.md) is ordered by priority and grouped by release. Items
marked 🟡 and 🟢 are the self-contained ones. Adding an ingestion adapter is the most
tractable meaningful change — the recipe is in `.claude/agent.md`.
