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
    keywords: list[str] = Field(default_factory=list)
    links: dict = Field(default_factory=dict)


class Watchlist(BaseModel):
    greenhouse: list[str] = Field(default_factory=list)
    lever: list[str] = Field(default_factory=list)
    ashby: list[str] = Field(default_factory=list)


class Preferences(BaseModel):
    profile: Profile = Field(default_factory=Profile)
    watchlist: Watchlist = Field(default_factory=Watchlist)


def load_preferences(path: str = DEFAULT_PATH) -> Preferences:
    p = Path(path)
    if not p.exists():
        return Preferences()
    data = tomllib.loads(p.read_text())
    return Preferences(**data)
