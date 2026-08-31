"""Aggregate recurring skill gaps across scored matches into an upskilling report.

Borrowed in spirit from ai-job-search's `/upskill` (MIT): weight each job's gaps by how
poorly it fit — a weaker match exposes more to learn — rank the gaps that recur, and turn
the top ones into a study plan. Adapted to this system's stored `Match.gaps`, which the
heuristic and LLM scorers already produce (`matching/heuristic.py`, `matching/llm.py`).

The heavy lifting (`skill_gaps`, `structural_notes`) is pure and offline-testable (R17);
`learning_plan` is the only part that calls a model, and it takes an injected `llm`.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field

# Gaps the scorers emit that are not "learn a skill" items — they describe why a posting
# filtered the candidate out, not a competency to acquire. Kept as a separate signal.
_STRUCTURAL = (
    (re.compile(r"^not clearly remote"), "location/remote"),
    (re.compile(r"^excluded:"), "hard-exclusion"),
    (re.compile(r"seniorit|senior|junior|lead|principal|years"), "seniority"),
)
_MUSTHAVE_RE = re.compile(r"^must[- ]?have not found:\s*", re.IGNORECASE)


@dataclass
class SkillGap:
    skill: str
    weight: float          # Σ (1 − score) over the jobs that exposed it
    count: int             # how many jobs exposed it
    examples: list[str] = field(default_factory=list)  # up to 3 "Title @ Company"

    def as_dict(self) -> dict:
        return {"skill": self.skill, "weight": round(self.weight, 3),
                "count": self.count, "examples": self.examples}


def _decode_gaps(row: dict) -> list[str]:
    """`gaps` is stored as JSON text but a caller may hand us an already-decoded list."""
    raw = row.get("gaps")
    if isinstance(raw, list):
        return [str(g) for g in raw]
    try:
        val = json.loads(raw or "[]")
        return [str(g) for g in val] if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _classify(gap: str) -> tuple[str, str]:
    """Return (kind, label). kind is 'skill' or 'structural'; label is the normalized
    skill name (must-have prefix stripped) or the structural category."""
    g = gap.strip()
    low = g.lower()
    for pattern, label in _STRUCTURAL:
        if pattern.search(low):
            return "structural", label
    skill = _MUSTHAVE_RE.sub("", g).strip().lower()
    return "skill", skill or low


def _label(row: dict) -> str:
    return f"{row.get('title') or 'role'} @ {row.get('company') or '—'}"


def skill_gaps(matches: list[dict], *, min_score: float = 0.5, limit: int = 20) -> list[SkillGap]:
    """Rank recurring *skill* gaps. A job contributes weight `1 − score`, so a moderate
    fit (which the candidate could realistically close) counts more than a weak one; a
    perfect fit contributes to the count but adds no weight. Structural gaps are excluded
    here — see `structural_notes`."""
    weight: dict[str, float] = defaultdict(float)
    count: dict[str, int] = defaultdict(int)
    examples: dict[str, list[tuple[float, str]]] = defaultdict(list)

    for row in matches:
        score = float(row.get("score") or 0.0)
        if score < min_score:
            continue
        w = max(0.0, 1.0 - score)
        for gap in _decode_gaps(row):
            kind, label = _classify(gap)
            if kind != "skill" or not label:
                continue
            weight[label] += w
            count[label] += 1
            examples[label].append((w, _label(row)))

    gaps = [
        SkillGap(
            skill=skill,
            weight=weight[skill],
            count=count[skill],
            examples=[lbl for _, lbl in sorted(examples[skill], reverse=True)[:3]],
        )
        for skill in weight
    ]
    # Recurring-and-closeable first. Two stable sorts: name ascending as the tiebreak,
    # then weight+count descending wins — deterministic regardless of dict order.
    gaps.sort(key=lambda g: g.skill)
    gaps.sort(key=lambda g: (round(g.weight, 6), g.count), reverse=True)
    return gaps[:limit]


def structural_notes(matches: list[dict], *, min_score: float = 0.5) -> dict[str, int]:
    """Count the non-skill reasons postings filtered the candidate out (seniority,
    location, hard-exclusions). Not upskilling targets, but worth surfacing."""
    notes: dict[str, int] = defaultdict(int)
    for row in matches:
        if float(row.get("score") or 0.0) < min_score:
            continue
        for gap in _decode_gaps(row):
            kind, label = _classify(gap)
            if kind == "structural":
                notes[label] += 1
    return dict(notes)


def upskill_report(store, *, min_score: float = 0.5, limit: int = 20, pool: int = 1000) -> dict:
    """Pull scored matches from the store and build the full gap report (no model)."""
    matches = store.get_matches(limit=pool, min_score=min_score)
    gaps = skill_gaps(matches, min_score=min_score, limit=limit)
    return {
        "n_jobs": len(matches),
        "min_score": min_score,
        "gaps": [g.as_dict() for g in gaps],
        "structural": structural_notes(matches, min_score=min_score),
    }


# --- learning plan (the one model-backed step) ----------------------------------

PLAN_SYSTEM = (
    "You are a career-development coach. Given a candidate's current skills and a ranked "
    "list of skill GAPS drawn from real job postings they were matched against, produce a "
    "concrete, prioritized learning plan. For each of the top gaps: why it matters for the "
    "roles they target, a suggested study order, one or two concrete resource types "
    "(course, docs, project), and a rough time estimate. Be honest and specific — do not "
    "pad, and do not claim the candidate already has a gap skill. Group into 'now / next / "
    "later'. Output clean Markdown."
)


def learning_plan_prompt(gaps: list[SkillGap], profile_skills: str) -> tuple[str, str]:
    gap_lines = "\n".join(
        f"- {g.skill} (in {g.count} job(s), priority {g.weight:.2f}; e.g. {', '.join(g.examples[:2])})"
        for g in gaps
    ) or "- (no recurring skill gaps found)"
    user = (
        f"CANDIDATE'S CURRENT SKILLS:\n{profile_skills or '(not provided)'}\n\n"
        f"RANKED SKILL GAPS (highest priority first):\n{gap_lines}\n\n"
        "Write the learning plan."
    )
    return PLAN_SYSTEM, user


def learning_plan(gaps: list[SkillGap], profile_skills: str, llm, *, top: int = 8) -> str:
    """Turn the top gaps into a Markdown study plan. Returns '' if there is nothing to plan."""
    top_gaps = gaps[:top]
    if not top_gaps:
        return ""
    system, user = learning_plan_prompt(top_gaps, profile_skills)
    return llm.complete(system, user).strip()
