"""Which profile fields the agent may change, and what a change would do.

Mirrors `config_policy` for the search profile. `PROFILE_WRITABLE` is search behaviour —
what to look for and where. Everything else on `Profile` is frozen by complement:
identity (name, email, phone, cv_path, links), because it is what gets typed into an
employer's form, and the CV, because it is R1's ground truth. Frozen-by-complement means
a field added to `Profile` next year is frozen the day it is added.

`preview_profile` shows the parsed value the operator is approving; `apply_profile`
writes only that one field into the gitignored `data/profile.json` overlay — the same
layer Settings edits — leaving every other field to the layers below (the
`exclude_unset` lesson: never write a field the caller did not name).
"""

from __future__ import annotations

from dataclasses import dataclass

from jobagent.preferences import Profile, load_preferences, save_overlay

# List-valued search fields: comma-separated free text becomes a clean list.
_LIST_FIELDS: frozenset[str] = frozenset({
    "target_roles", "core_skills", "domains", "must_haves", "nice_to_haves",
    "exclude_keywords", "preferred_locations", "exclude_locations", "keywords",
    "geo_global_terms", "geo_eligible", "geo_blocked",
})
# Scalar search fields.
_SCALAR_FIELDS: frozenset[str] = frozenset({
    "seniority", "work_mode", "location", "timezone", "remote_scope",
})

PROFILE_WRITABLE: frozenset[str] = _LIST_FIELDS | _SCALAR_FIELDS

# Everything else on Profile is frozen. Computed as the complement so a new field is
# frozen by default; `skill_weights` and `links` are dict-shaped and not delegable
# through a flat field/value tool, so they stay frozen here too (edit in Settings).
PROFILE_FROZEN: frozenset[str] = frozenset(Profile.model_fields) - PROFILE_WRITABLE

_IDENTITY: frozenset[str] = frozenset({"name", "email", "phone", "cv_path", "links"})


class ProfileRefused(Exception):
    """The change is not something to confirm — it is something to reject."""


@dataclass
class ProfileImpact:
    field: str
    current: object
    parsed: object

    def render(self) -> str:
        return (f"{self.field}: {self.current!r} → {self.parsed!r}\n"
                f"Run rematch afterwards so existing postings are re-scored.")


def _coerce(field: str, value: str):
    if field in _LIST_FIELDS:
        return [part.strip() for part in str(value).split(",") if part.strip()]
    return str(value).strip()


def _check(field: str) -> None:
    if field in PROFILE_WRITABLE:
        return
    if field in _IDENTITY:
        raise ProfileRefused(
            f"{field!r} is identity — it is typed into employer application forms, so the "
            f"agent never changes it. Edit it yourself in Settings → Profile.")
    if field in PROFILE_FROZEN:
        raise ProfileRefused(
            f"{field!r} is not an agent-writable search field (it is frozen: identity, the "
            f"CV, or a structured field like skill_weights). Edit it in Settings → Profile.")
    raise ProfileRefused(f"{field!r} is not a profile field.")


def preview_profile(field: str, value: str, *, local_path=None, overlay_path=None) -> ProfileImpact:
    _check(field)
    profile = load_preferences(local_path=local_path, overlay_path=overlay_path).profile
    return ProfileImpact(field=field, current=getattr(profile, field, None),
                         parsed=_coerce(field, value))


def apply_profile(field: str, value: str, *, overlay_path=None, local_path=None) -> str:
    _check(field)
    parsed = _coerce(field, value)
    save_overlay({"profile": {field: parsed}}, overlay_path=overlay_path)
    return f"Set {field} = {parsed!r}. Run rematch to re-score existing postings."
