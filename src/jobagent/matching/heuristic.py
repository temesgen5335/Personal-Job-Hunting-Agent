"""Transparent, dependency-free heuristic scorer.

Runs over every stored job with no API calls — gives an immediate ranked shortlist
and acts as the cheap prefilter before (optional) LLM reranking. Scoring is
explainable: the rationale lists exactly which signals fired.
"""

from __future__ import annotations

import json
import re

from jobagent.preferences import Profile


def _hits(terms: list[str], hay: str) -> list[str]:
    """Word-boundary match (alnum-aware) so 'Go' doesn't match 'going' and 'RAG'
    doesn't match 'fragment'. Handles multi-word terms and dots (Next.js)."""
    out = []
    for t in terms:
        pat = r"(?<![a-z0-9])" + re.escape(t.lower()) + r"(?![a-z0-9])"
        if re.search(pat, hay):
            out.append(t)
    return out


def heuristic_score(job: dict, profile: Profile) -> tuple[float, str, list[str]]:
    """Return (score 0..1, rationale, gaps) for one job row (dict from the store)."""
    title = (job.get("title") or "").lower()
    desc = (job.get("description") or "").lower()
    try:
        tags = json.loads(job.get("tags") or "[]")
    except (json.JSONDecodeError, TypeError):
        tags = []
    text = " ".join([title, desc, " ".join(t.lower() for t in tags)])

    # Role match is weighted on the title (and tags), where it's most meaningful.
    role_terms = profile.target_roles + profile.keywords
    role_hits = _hits(role_terms, title + " " + " ".join(tags).lower())
    kw_hits = _hits(profile.keywords, text)
    skill_hits = _hits(profile.core_skills, text)
    domain_hits = _hits(profile.domains, text)
    exclude_hits = _hits(profile.exclude_keywords, text)

    score = 0.0
    if role_hits:
        score += 0.40
    score += min(len(kw_hits), 6) * 0.05       # ≤0.30
    score += min(len(skill_hits), 10) * 0.025  # ≤0.25
    score += min(len(domain_hits), 3) * 0.04   # ≤0.12

    # Trust the structured flag and the location/title for remote — NOT the full
    # description, which is full of "remote-friendly culture" boilerplate.
    loc_title = (job.get("location") or "").lower() + " " + title
    remote_ok = bool(job.get("is_remote")) or any(
        w in loc_title for w in ("remote", "worldwide", "anywhere", "distributed")
    )
    gaps: list[str] = []
    if "remote" in [m.lower() for m in profile.must_haves]:
        if remote_ok:
            score += 0.06
        else:
            score -= 0.30
            gaps.append("not clearly remote")

    if exclude_hits:
        score = min(score, 0.15)  # hard down-rank, don't fully drop (let user see why)
        gaps.append("excluded: " + ", ".join(exclude_hits))

    score = max(0.0, min(1.0, score))

    parts = []
    if role_hits:
        parts.append("role/keywords in title: " + ", ".join(sorted(set(role_hits))[:5]))
    if skill_hits:
        parts.append("skills: " + ", ".join(sorted(set(skill_hits))[:6]))
    if domain_hits:
        parts.append("domains: " + ", ".join(sorted(set(domain_hits))))
    parts.append("remote" if remote_ok else "location unclear")
    rationale = "; ".join(parts)

    return round(score, 3), rationale, gaps
