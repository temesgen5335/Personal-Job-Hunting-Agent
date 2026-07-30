"""Transparent, dependency-free heuristic scorer.

Runs over every stored job with no API calls — gives an immediate ranked shortlist
and acts as the cheap prefilter before (optional) LLM reranking. Scoring is
explainable: the rationale lists exactly which signals fired.

Scoring is *preference-weighted* rather than a flat keyword count, because a flat
count is what produces false positives at the top of the list:

  - Role signal distinguishes a target-role match in the TITLE from an incidental
    keyword appearing somewhere in the body. Previously any single role term in the
    title earned the full role bonus, so "Sales Engineer" scored like "AI Engineer".
  - Skills are weighted (`profile.skill_weights`), so matching four differentiating
    skills outranks matching four generic infra tools. Coverage saturates rather than
    normalizing over the whole skill list, which would punish every posting for not
    mentioning all of them.
  - Seniority mismatch is penalized: a "Junior" or "Head of Engineering" posting is a
    false positive for a mid-to-senior IC however well its keywords match.
  - Every must-have is checked, not just "remote".
"""

from __future__ import annotations

import json
import re

from jobagent.preferences import Profile

# Component ceilings. They sum to 0.92, leaving 0.08 for the remote bonus so a
# fully-matching remote posting can reach 1.0.
_W_ROLE = 0.35
_W_SKILL = 0.30
_W_KEYWORD = 0.15
_W_DOMAIN = 0.12
_W_REMOTE_BONUS = 0.08

# Skill weight at which coverage counts as complete. Six default-weight skills
# saturate; with weighting, three heavy ones can. Postings mention a handful of
# technologies, so normalizing over the entire profile list would flatten everything.
_SKILL_SATURATION = 6.0
_KEYWORD_SATURATION = 6
_DOMAIN_SATURATION = 3

_PENALTY_NOT_REMOTE = 0.30
_PENALTY_MUST_HAVE = 0.10
_PENALTY_SENIORITY = 0.20
_EXCLUDED_CEILING = 0.15

# Titles that mismatch a mid/senior individual-contributor profile. Kept deliberately
# narrow: a wrong penalty buries a good job, so only unambiguous signals belong here.
_TOO_JUNIOR = (
    "intern", "internship", "junior", "graduate", "entry level", "entry-level",
    "apprentice", "trainee", "working student",
)
_MANAGEMENT_TRACK = ("director", "vice president", "head of", "chief", "cto", "vp of")
# NOTE: both tuples are matched with _hits (word boundaries), never `in`. Substring
# matching flagged "SemiconduCTOrs" and "ConneCTOrs" as CTO roles — the same class of
# bug that made "Go" match "going" before _hits existed.
_REMOTE_WORDS = ("remote", "worldwide", "anywhere", "distributed")


def _hits(terms: list[str], hay: str) -> list[str]:
    """Word-boundary match (alnum-aware) so 'Go' doesn't match 'going' and 'RAG'
    doesn't match 'fragment'. Handles multi-word terms and dots (Next.js)."""
    out = []
    for t in terms:
        pat = r"(?<![a-z0-9])" + re.escape(t.lower()) + r"(?![a-z0-9])"
        if re.search(pat, hay):
            out.append(t)
    return out


def _weight_of(skill: str, weights: dict[str, float]) -> float:
    """Skill weight, case-insensitive, default 1.0.

    Non-numeric weights are rejected by Profile validation at load time, so a config
    typo fails loudly rather than silently skewing every score. Negatives are clamped
    to 0 here: they pass validation but would turn a match into a penalty.
    """
    if not weights:
        return 1.0
    lowered = {k.lower(): v for k, v in weights.items()}
    return max(0.0, lowered.get(skill.lower(), 1.0))


def _role_signal(title: str, tags_text: str, text: str, profile: Profile) -> tuple[float, str]:
    """How strongly this posting *is* one of the target roles.

    A target role in the title is the strongest signal available; a loose keyword in
    the title is weaker; a target role buried in the body is weakest.
    """
    title_zone = f"{title} {tags_text}"
    if _hits(profile.target_roles, title_zone):
        return 1.0, "target role in title"
    if _hits(profile.keywords, title_zone):
        return 0.6, "keyword in title"
    if _hits(profile.target_roles, text):
        return 0.3, "target role in body"
    return 0.0, ""


def _seniority_gap(title: str, profile: Profile) -> str | None:
    """Return a gap description if the posting's level clearly mismatches the profile."""
    seniority = (profile.seniority or "").lower()
    wants_ic = not any(w in seniority for w in ("manager", "director", "lead", "head"))
    entry_level_profile = any(w in seniority for w in ("junior", "entry", "graduate", "intern"))

    if not entry_level_profile:
        junior = _hits(list(_TOO_JUNIOR), title)
        if junior:
            return f"level mismatch: '{junior[0]}' role"
    if wants_ic:
        mgmt = _hits(list(_MANAGEMENT_TRACK), title)
        if mgmt:
            return f"management-track role ('{mgmt[0]}')"
    return None


def heuristic_score(job: dict, profile: Profile) -> tuple[float, str, list[str]]:
    """Return (score 0..1, rationale, gaps) for one job row (dict from the store)."""
    title = (job.get("title") or "").lower()
    desc = (job.get("description") or "").lower()
    try:
        tags = json.loads(job.get("tags") or "[]")
    except (json.JSONDecodeError, TypeError):
        tags = []
    tags_text = " ".join(str(t).lower() for t in tags)
    text = " ".join([title, desc, tags_text])

    gaps: list[str] = []

    # --- positive signals ---------------------------------------------------------
    role_strength, role_why = _role_signal(title, tags_text, text, profile)

    skill_hits = _hits(profile.core_skills, text)
    matched_weight = sum(_weight_of(s, profile.skill_weights) for s in skill_hits)
    skill_cover = min(1.0, matched_weight / _SKILL_SATURATION)

    kw_hits = _hits(profile.keywords, text)
    domain_hits = _hits(profile.domains, text)

    score = (
        _W_ROLE * role_strength
        + _W_SKILL * skill_cover
        + _W_KEYWORD * (min(len(kw_hits), _KEYWORD_SATURATION) / _KEYWORD_SATURATION)
        + _W_DOMAIN * (min(len(domain_hits), _DOMAIN_SATURATION) / _DOMAIN_SATURATION)
    )

    # --- must-haves ---------------------------------------------------------------
    # Trust the structured flag and the location/title for remote — NOT the full
    # description, which is full of "remote-friendly culture" boilerplate.
    loc_title = (job.get("location") or "").lower() + " " + title
    remote_ok = bool(job.get("is_remote")) or any(w in loc_title for w in _REMOTE_WORDS)

    for must in profile.must_haves:
        if must.lower() == "remote":
            if remote_ok:
                score += _W_REMOTE_BONUS
            else:
                score -= _PENALTY_NOT_REMOTE
                gaps.append("not clearly remote")
        elif not _hits([must], text):
            # Other must-haves are prose ("async-friendly", "AI-native"); absence is
            # weak evidence rather than proof, so penalize lightly but name the gap.
            score -= _PENALTY_MUST_HAVE
            gaps.append(f"must-have not found: {must}")

    # --- level mismatch -----------------------------------------------------------
    seniority_gap = _seniority_gap(title, profile)
    if seniority_gap is not None:
        score -= _PENALTY_SENIORITY
        gaps.append(seniority_gap)

    # --- exclusions ---------------------------------------------------------------
    exclude_hits = _hits(profile.exclude_keywords, text)
    if exclude_hits:
        # Hard down-rank rather than dropping: the user still sees the posting and why.
        score = min(score, _EXCLUDED_CEILING)
        gaps.append("excluded: " + ", ".join(exclude_hits))

    score = max(0.0, min(1.0, score))

    # --- rationale ----------------------------------------------------------------
    parts = []
    if role_why:
        parts.append(role_why)
    if skill_hits:
        # Heaviest-weighted matches first — those are the ones worth reading.
        ranked = sorted(set(skill_hits), key=lambda s: -_weight_of(s, profile.skill_weights))
        parts.append("skills: " + ", ".join(ranked[:6]))
    if domain_hits:
        parts.append("domains: " + ", ".join(sorted(set(domain_hits))))
    parts.append("remote" if remote_ok else "location unclear")
    rationale = "; ".join(parts)

    return round(score, 3), rationale, gaps
