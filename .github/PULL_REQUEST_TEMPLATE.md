## What and why

<!-- What changes, and what problem it solves. The "why" is the part reviewers need. -->

## How it was verified

<!--
`make test` passing is necessary, not sufficient. This project's history is mostly bugs
a green suite could not see. If the change touches an LLM prompt, a live source, a
browser surface, or real data shapes, say what you actually ran.
-->

- [ ] `make test` passes (`tests/test_docs.py` included — it fails on stale doc claims)
- [ ] New/changed behaviour has a test that **fails without the change**
- [ ] Docs updated in this same change (when docs and code disagree, the code wins)
- [ ] `CHANGELOG.md` has an `## [Unreleased]` entry

## Rules checked

- [ ] No path submits an application without explicit approval (R2)
- [ ] No generator makes claims about the candidate without receiving the CV (R1/R1a)
- [ ] Any new non-GET route carries `dependencies=auth` (R19)
- [ ] No new tool that must never exist was added-and-gated rather than omitted (R26)
