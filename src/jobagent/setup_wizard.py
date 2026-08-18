"""First-run setup: turn a fresh clone into a configured install.

The onboarding this replaces was chicken-and-egg. Settings can edit the whole profile,
but `DASHBOARD_PASSWORD` had to already be in `.env` before any write worked, and the
API had to already be running — so the "no file editing needed" story only began *after*
manual file editing.

Everything here is a pure function over an `Answers` object. `scripts/setup.py` is the
only part that touches stdin, so the logic is testable without driving a terminal and
without a test ever writing to the developer's real `.env`.

Two rules the wizard must never break:
  1. **Never clobber.** An existing value is kept unless the operator says otherwise;
     an existing `.env` is updated key-by-key, not rewritten.
  2. **Never echo a secret.** Prompts confirm that a key was set, never what it is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Only what a first run actually needs. Everything else in .env.example is optional and
# discoverable later — a wizard that asks 40 questions gets abandoned at question 6.
ENV_TEMPLATE = "\n".join([
    "# Written by `make setup`. Every key is documented in .env.example.",
    "# This file is gitignored and holds live credentials — never commit it.",
    "",
])


@dataclass
class Answers:
    """Everything the wizard collects. Defaults are the 'just press enter' path."""

    name: str = ""
    headline: str = ""
    email: str = ""
    target_roles: list[str] = field(default_factory=list)
    core_skills: list[str] = field(default_factory=list)
    location: str = ""
    remote_only: bool = True
    seniority: str = ""
    dashboard_password: str = ""
    llm_provider: str = ""
    llm_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""


def split_list(raw: str) -> list[str]:
    """Comma-separated free text → a clean list. Empty entries are dropped rather than
    stored, because an empty skill silently matches nothing and looks like a bug."""
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def parse_env(text: str) -> dict[str, str]:
    """Read a .env into a dict, preserving nothing else. Comments and blanks are skipped.

    Deliberately tolerant: a hand-edited .env with odd spacing must not break setup.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
            out[key] = value.strip().strip('"').strip("'")
    return out


def merge_env(existing: str, updates: dict[str, str]) -> str:
    """Apply `updates` to an existing .env body, preserving comments, order and any key
    the wizard does not manage.

    A rewrite-from-scratch would silently drop SMTP settings, model overrides, and every
    other key someone had already tuned — the single most annoying thing a setup script
    can do.
    """
    updates = {k: v for k, v in updates.items() if v != ""}
    lines = existing.splitlines()
    seen: set[str] = set()

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.partition("=")[0].strip()
        if key in updates:
            lines[i] = f"{key}={updates[key]}"
            seen.add(key)

    missing = [f"{k}={v}" for k, v in updates.items() if k not in seen]
    if missing:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(missing)
    return "\n".join(lines).rstrip() + "\n"


def env_updates(answers: Answers, *, master_key: str) -> dict[str, str]:
    """The .env keys a first run needs. Blank answers are omitted, not written as
    empty — an explicit empty value would shadow a real one set elsewhere."""
    updates = {
        "JOBAGENT_MASTER_KEY": master_key,
        "DASHBOARD_PASSWORD": answers.dashboard_password,
        "TELEGRAM_BOT_TOKEN": answers.telegram_bot_token,
        "TELEGRAM_CHAT_ID": answers.telegram_chat_id,
    }
    if answers.llm_provider and answers.llm_api_key:
        updates["LLM_PROVIDER"] = answers.llm_provider
        updates[f"{answers.llm_provider.upper()}_API_KEY"] = answers.llm_api_key
    return {k: v for k, v in updates.items() if v}


def profile_overlay(answers: Answers, existing: dict | None = None) -> dict:
    """The `data/profile.json` overlay — the layer the dashboard also writes.

    The wizard writes HERE rather than to `config/preferences.toml` on purpose: it is the
    same layer Settings edits, so answering here and editing in the browser later are the
    same act, and the shipped template stays pristine underneath as a fallback.
    """
    profile = dict((existing or {}).get("profile", {}))
    for key, value in (
        ("name", answers.name),
        ("headline", answers.headline),
        ("email", answers.email),
        ("location", answers.location),
        ("seniority", answers.seniority),
        ("target_roles", answers.target_roles),
        ("core_skills", answers.core_skills),
    ):
        if value:
            profile[key] = value

    if answers.remote_only:
        profile["work_mode"] = "remote"
        profile["must_haves"] = sorted(set(profile.get("must_haves", []) + ["remote"]))

    # Skills the operator named are what they want to be hired for, so they start
    # weighted above the 1.0 default. Generic infrastructure they add later stays at 1.0
    # until they tune it — the point of weights is to separate the two.
    if answers.core_skills:
        weights = dict(profile.get("skill_weights", {}))
        for skill in answers.core_skills:
            weights.setdefault(skill, 2.0)
        profile["skill_weights"] = weights

    merged = dict(existing or {})
    merged["profile"] = profile
    return merged


def next_steps(answers: Answers, *, has_llm: bool, has_telegram: bool) -> list[str]:
    """What to actually run next. A setup script that ends without telling you what it
    unlocked leaves you back at the README."""
    steps = ["make pipeline    # ingest + match — works right now, no credentials needed",
             "make run         # API on :8077, dashboard on :1234"]
    if not has_llm:
        steps.append("Optional: add an LLM key in Settings → LLM to enable "
                     "CV tailoring and cover letters (matching already works without one)")
    if not has_telegram:
        steps.append("Optional: add a Telegram bot token in Settings → Telegram "
                     "for the daily digest and the /jobs bot")
    return steps
