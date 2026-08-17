"""Load — and now write — the job-search profile, watchlist and source toggles.

Three layers, lowest priority first, merged section-wise:

  1. `config/preferences.toml`      committed placeholders — shareable, no PII
  2. `config/preferences.local.toml` legacy gitignored overlay (kept for back-compat)
  3. `data/profile.json`            the writable overlay the dashboard edits

Only layer 3 is ever written, and it lives under the gitignored `data/` dir, so a
person's real identity, background and preferences are *referenced* by the running
system and never hardcoded into the tree. This matches the posture that already
existed (the CV and `preferences.local.toml` were gitignored plaintext) — it just
makes the overlay editable through the UI instead of only by hand.

The CV master is the same idea: read from `data/cv_master.md` if present, else the
legacy `config/cv_master.md`, and written only to the `data/` copy.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_PATH = "config/preferences.toml"
# The writable overlay + CV. Env-overridable so tests are hermetic (a test must never
# read or write the developer's real data/ — the same discipline SecretStore uses for
# JOBAGENT_SECRETS_PATH).
OVERLAY_PATH = "data/profile.json"
CV_PATH = "data/cv_master.md"
LEGACY_CV_PATH = "config/cv_master.md"


class Profile(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = ""
    headline: str = ""
    cv_path: str = ""
    email: str = ""      # used to fill ATS application forms (Phase 4)
    phone: str = ""
    target_roles: list[str] = Field(default_factory=list)
    seniority: str = ""
    work_mode: str = ""
    location: str = ""
    timezone: str = ""
    core_skills: list[str] = Field(default_factory=list)
    # Optional per-skill importance, keyed by the same string used in core_skills
    # (case-insensitive). Unlisted skills weigh 1.0. Weighting is what separates
    # "matched four of my differentiators" from "matched four generic infra tools" —
    # without it every skill counts the same and generic postings score like strong ones.
    skill_weights: dict[str, float] = Field(default_factory=dict)
    domains: list[str] = Field(default_factory=list)
    must_haves: list[str] = Field(default_factory=list)
    nice_to_haves: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    # Location filtering. preferred_locations: if set, keep only jobs matching one.
    # exclude_locations: always drop jobs whose location matches any (e.g. "US only").
    preferred_locations: list[str] = Field(default_factory=list)
    exclude_locations: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    links: dict = Field(default_factory=dict)


class Watchlist(BaseModel):
    greenhouse: list[str] = Field(default_factory=list)
    lever: list[str] = Field(default_factory=list)
    ashby: list[str] = Field(default_factory=list)


class Sources(BaseModel):
    """Per-source on/off switches. Default ON so existing behavior is unchanged.
    Flip to false to stop ingesting from a source without deleting its config."""

    remoteok: bool = True
    remotive: bool = True
    greenhouse: bool = True
    lever: bool = True
    ashby: bool = True
    telegram: bool = True
    aggregator: bool = False   # LinkedIn/Indeed/Glassdoor via JSearch/SerpApi (needs key + adapter)

    def is_enabled(self, source: str) -> bool:
        return bool(getattr(self, source, True))


class Preferences(BaseModel):
    profile: Profile = Field(default_factory=Profile)
    watchlist: Watchlist = Field(default_factory=Watchlist)
    sources: Sources = Field(default_factory=Sources)


def _merge(base: dict, overlay: dict) -> dict:
    """Section-wise merge: overlay keys win, one level deep (matching the TOML shape)."""
    out = dict(base)
    for section, values in overlay.items():
        if isinstance(values, dict) and isinstance(out.get(section), dict):
            out[section] = {**out[section], **values}
        else:
            out[section] = values
    return out


def _overlay_path(explicit: str | None = None) -> Path:
    return Path(explicit or os.environ.get("JOBAGENT_PROFILE_PATH", OVERLAY_PATH))


def _cv_paths(explicit: str | None = None) -> tuple[Path, Path | None]:
    """(writable data path, legacy config path or None).

    The legacy `config/cv_master.md` fallback applies ONLY on the default path. An
    explicit override — `JOBAGENT_CV_PATH` or an argument — is authoritative and does
    not fall back, so a test pointing at an empty tmp dir sees no CV, not the
    developer's real one. Presence, not truthiness: the same discipline as R31.
    """
    override = explicit or os.environ.get("JOBAGENT_CV_PATH")
    if override:
        return Path(override), None
    return Path(CV_PATH), Path(LEGACY_CV_PATH)


def load_preferences(
    path: str = DEFAULT_PATH,
    local_path: str | None = None,
    overlay_path: str | None = None,
) -> Preferences:
    """Load preferences, merging the three layers described in the module docstring.

    Committed placeholders lose to the legacy `.local.toml`, which loses to the
    writable `data/profile.json`. A clone with no overlays still yields a usable
    (placeholder) profile; a configured install yields the operator's real one.
    """
    base = tomllib.loads(Path(path).read_text()) if Path(path).exists() else {}

    local = Path(local_path or str(path).replace(".toml", ".local.toml"))
    if local.exists():
        base = _merge(base, tomllib.loads(local.read_text()))

    overlay = _overlay_path(overlay_path)
    if overlay.exists():
        try:
            base = _merge(base, json.loads(overlay.read_text() or "{}"))
        except (ValueError, TypeError):
            pass  # a corrupt overlay must not brick every request; placeholders win

    return Preferences(**base) if base else Preferences()


def load_overlay(overlay_path: str | None = None) -> dict:
    """The raw writable overlay only (not merged), for the settings UI to edit."""
    p = _overlay_path(overlay_path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text() or "{}")
    except (ValueError, TypeError):
        return {}


def save_overlay(patch: dict, overlay_path: str | None = None) -> dict:
    """Merge a patch into the writable overlay and persist it. Returns the new overlay.

    Section-wise merge (profile / watchlist / sources), so saving one tab never wipes
    another. A section value of null clears that section back to the lower layers.
    """
    p = _overlay_path(overlay_path)
    current = load_overlay(overlay_path)
    for section, value in (patch or {}).items():
        if value is None:
            current.pop(section, None)
        elif isinstance(value, dict) and isinstance(current.get(section), dict):
            current[section] = {**current[section], **value}
        else:
            current[section] = value
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(current, indent=2, ensure_ascii=False))
    return current


def load_cv_master(cv_path: str | None = None) -> str:
    """The CV master text: the writable `data/` copy if present, else the legacy one."""
    data, legacy = _cv_paths(cv_path)
    if data.exists():
        return data.read_text()
    if legacy is not None and legacy.exists():
        return legacy.read_text()
    return ""


def save_cv_master(text: str, cv_path: str | None = None) -> None:
    data, _ = _cv_paths(cv_path)
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_text(text or "")
