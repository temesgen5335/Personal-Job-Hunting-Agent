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
from dataclasses import dataclass, field, fields
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
    phone: str = ""
    timezone: str = ""
    domains: list[str] = field(default_factory=list)
    remote_scope: str = "any"          # "any" | "global"
    geo_eligible: list[str] = field(default_factory=list)
    geo_blocked: list[str] = field(default_factory=list)
    sources: dict[str, bool] = field(default_factory=dict)          # per-source toggles
    watchlist: dict[str, list[str]] = field(default_factory=dict)   # greenhouse/lever/ashby slugs
    llm_model: str = ""
    keyless: bool = False
    pollinations_enabled: bool = False
    smtp_host: str = ""
    smtp_port: str = ""
    smtp_user: str = ""
    smtp_password: str = ""
    apply_from_email: str = ""
    telegram_owner_id: str = ""


def split_list(raw: str) -> list[str]:
    """Comma-separated free text → a clean list. Empty entries are dropped rather than
    stored, because an empty skill silently matches nothing and looks like a bug."""
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


# List-valued Answers fields — a config file may give these as a list or a comma-string.
_LIST_FIELDS = ("target_roles", "core_skills", "domains", "geo_eligible", "geo_blocked")


def answers_from_mapping(data: dict) -> Answers:
    """Build Answers from a plain mapping (a --config JSON), so onboarding can run fully
    non-interactively. List fields accept a list or a comma-string; unknown keys are
    ignored; missing keys keep their Answers default."""
    known = {f.name for f in fields(Answers)}
    kwargs: dict = {}
    for key, value in (data or {}).items():
        if key not in known:
            continue
        if key in _LIST_FIELDS:
            if isinstance(value, str):
                value = split_list(value)
            elif value is None:
                value = []
            elif isinstance(value, list):
                value = list(value)
        elif isinstance(value, list):
            value = list(value)
        elif isinstance(value, dict):
            value = dict(value)
        kwargs[key] = value
    return Answers(**kwargs)


ONBOARD_EXAMPLE: dict = {
    "name": "Your Name", "headline": "Your one-line headline", "email": "you@example.com",
    "phone": "", "location": "Your City, Country", "timezone": "UTC+0",
    "target_roles": ["Software Engineer", "Backend Engineer"],
    "core_skills": ["Python", "SQL"], "domains": ["developer tools"], "seniority": "mid",
    "remote_only": True, "remote_scope": "any", "geo_eligible": [], "geo_blocked": [],
    "sources": {"remoteok": True, "remotive": True, "himalayas": True, "greenhouse": True,
                "lever": True, "ashby": True, "telegram": False},
    "watchlist": {"greenhouse": [], "lever": [], "ashby": []},
    "keyless": True, "pollinations_enabled": False,
    "llm_provider": "", "llm_api_key": "", "llm_model": "",
    "smtp_host": "", "smtp_port": "", "smtp_user": "", "smtp_password": "", "apply_from_email": "",
    "telegram_bot_token": "", "telegram_chat_id": "", "telegram_owner_id": "",
    "dashboard_password": "",
}


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
    """The .env keys onboarding writes. Blank answers are omitted, not written as empty —
    an explicit empty value would shadow a real one set elsewhere."""
    updates = {
        "JOBAGENT_MASTER_KEY": master_key,
        "DASHBOARD_PASSWORD": answers.dashboard_password,
        "TELEGRAM_BOT_TOKEN": answers.telegram_bot_token,
        "TELEGRAM_CHAT_ID": answers.telegram_chat_id,
        "TELEGRAM_OWNER_ID": answers.telegram_owner_id,
        "SMTP_HOST": answers.smtp_host,
        "SMTP_PORT": answers.smtp_port,
        "SMTP_USER": answers.smtp_user,
        "SMTP_PASSWORD": answers.smtp_password,
        "APPLY_FROM_EMAIL": answers.apply_from_email,
    }
    if answers.keyless and not answers.llm_api_key:
        # Keyless path: no provider key; optionally turn on the keyless Pollinations backend
        # so drafting works without a key (matching already works heuristic-only).
        if answers.pollinations_enabled:
            updates["POLLINATIONS_ENABLED"] = "true"
    elif answers.llm_provider and answers.llm_api_key:
        updates["LLM_PROVIDER"] = answers.llm_provider
        updates[f"{answers.llm_provider.upper()}_API_KEY"] = answers.llm_api_key
        if answers.llm_model:
            updates[f"{answers.llm_provider.upper()}_MODEL"] = answers.llm_model
    return {k: v for k, v in updates.items() if v}


def bootstrap_env(existing: str, *, password: str, master_key: str) -> str:
    """Ensure a `.env` has the two secrets a keyless first run needs, without clobbering
    anything already set. `password` and `master_key` are injected (generated by the
    caller) so this stays pure and testable. Returns the new .env body."""
    have = parse_env(existing)
    updates: dict[str, str] = {}
    if not have.get("DASHBOARD_PASSWORD"):
        updates["DASHBOARD_PASSWORD"] = password
    if not have.get("JOBAGENT_MASTER_KEY"):
        updates["JOBAGENT_MASTER_KEY"] = master_key
    return merge_env(existing, updates) if updates else existing


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

    for key, value in (
        ("phone", answers.phone),
        ("timezone", answers.timezone),
        ("domains", answers.domains),
        ("geo_eligible", answers.geo_eligible),
        ("geo_blocked", answers.geo_blocked),
    ):
        if value:
            profile[key] = value
    if answers.remote_scope == "global":
        profile["remote_scope"] = answers.remote_scope

    merged = dict(existing or {})
    merged["profile"] = profile
    if answers.sources:
        merged["sources"] = {**dict(merged.get("sources", {})), **answers.sources}
    if answers.watchlist:
        merged["watchlist"] = {**dict(merged.get("watchlist", {})), **answers.watchlist}
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
