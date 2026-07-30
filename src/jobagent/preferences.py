"""Load the job-search profile + company watchlist from config/preferences.toml.

Uses stdlib tomllib (read-only) — no extra dependency. Phase 2 matching consumes
Profile; ingestion consumes Watchlist.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_PATH = "config/preferences.toml"


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


def load_preferences(path: str = DEFAULT_PATH, local_path: str | None = None) -> Preferences:
    """Load preferences, overlaid by a gitignored local file if present.

    `preferences.toml` is committed and holds shareable search config (target roles,
    skills, watchlist, source toggles). `preferences.local.toml` is gitignored and
    holds identity — name, email, phone, cv_path — so a clone of this repo carries a
    working search profile without carrying anyone's personal contact details.
    Same split as `.env.example` vs `.env`.
    """
    base = tomllib.loads(Path(path).read_text()) if Path(path).exists() else {}
    local = Path(local_path or str(path).replace(".toml", ".local.toml"))
    if local.exists():
        base = _merge(base, tomllib.loads(local.read_text()))
    return Preferences(**base) if base else Preferences()
