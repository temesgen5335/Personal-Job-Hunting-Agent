"""Fit-checker — how well the candidate's CV/profile fits a specific job.

Two modes, mirroring matching:
- heuristic_fit: ATS-style keyword coverage (which skills the JD wants are in your CV),
  no API. Always available.
- llm_fit: an explainable report (matched/missing requirements, experience read,
  confidence) via the shared LLM. Used when a model is configured.

assess_fit() returns the LLM report when possible, else the heuristic one.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from jobagent.matching.heuristic import _hits
from jobagent.preferences import Profile


@dataclass
class FitReport:
    score: float                       # 0..1 confidence
    coverage: float                    # 0..1 keyword coverage (skills wanted ∩ in CV)
    matched: list[str] = field(default_factory=list)   # relevant skills present in CV
    missing: list[str] = field(default_factory=list)   # skills the JD wants, not in CV
    experience: str = ""
    summary: str = ""
    source: str = "heuristic"          # "heuristic" | "llm"
    location_fit: bool = True          # does the job location suit the candidate?
    location_note: str = ""            # e.g. "remote ✓", "excluded: US only"

    def pct(self) -> int:
        return round(self.score * 100)

    def to_dict(self) -> dict:
        return asdict(self)

    def format_short(self) -> str:
        lines = [f"🎯 *Fit: {self.pct()}%* ({self.source})"]
        lines.append(f"✅ Matched: {', '.join(self.matched[:10]) or '—'}")
        lines.append(f"⚠️ Gaps: {', '.join(self.missing[:10]) or 'none'}")
        lines.append(f"📍 Location: {self.location_note or ('fits' if self.location_fit else 'mismatch')}")
        if self.experience:
            lines.append(f"🧭 {self.experience}")
        if self.summary:
            lines.append(self.summary)
        return "\n".join(lines)


def _location_fit(job: dict, profile) -> tuple[bool, str]:
    loc = (job.get("location") or "").lower()
    remote_pref = "remote" in [m.lower() for m in profile.must_haves]
    job_remote = bool(job.get("is_remote")) or any(w in loc for w in ("remote", "worldwide", "anywhere"))
    if any(x.lower() in loc for x in profile.exclude_locations):
        return False, f"excluded: {loc or 'n/a'}"
    if remote_pref and not job_remote:
        return False, f"not remote ({loc or 'unspecified'})"
    return True, "remote ✓" if job_remote else (loc or "unspecified")


def heuristic_fit(job: dict, profile: Profile, cv_text: str) -> FitReport:
    title = (job.get("title") or "").lower()
    jd = " ".join([title, (job.get("description") or "").lower()])
    cv = (cv_text or "").lower()

    vocab = list(dict.fromkeys(profile.core_skills + profile.keywords))  # de-duped, ordered
    jd_skills = _hits(vocab, jd)                       # skills the JD references
    present = _hits(jd_skills, cv)                      # …that are also in the CV
    missing = [s for s in jd_skills if s not in present]
    coverage = len(present) / len(jd_skills) if jd_skills else 0.0

    role_hit = bool(_hits(profile.target_roles + profile.keywords, title))
    loc_fit, loc_note = _location_fit(job, profile)
    score = coverage * 0.55 + (0.25 if role_hit else 0.0) + (0.12 if jd_skills else 0.0) + (0.08 if loc_fit else 0.0)
    score = max(0.0, min(1.0, round(score, 3)))

    summary = f"{len(present)}/{len(jd_skills)} relevant skills found in your CV." if jd_skills else \
        "No clearly recognizable skill overlap — likely a weak match."
    return FitReport(score=score, coverage=round(coverage, 3), matched=present,
                     missing=missing, summary=summary, source="heuristic",
                     location_fit=loc_fit, location_note=loc_note)


_SYSTEM = (
    "You assess how well a candidate fits a job. Return STRICT JSON: "
    '{"confidence": <0..1>, "matched": ["..."], "missing": ["..."], '
    '"experience": "<one-line seniority/experience read>", "summary": "<one sentence>"}. '
    "matched = key requirements the candidate clearly meets; missing = requirements they "
    "lack. Be honest; do not inflate. Base it ONLY on the provided CV."
)


def llm_fit(job: dict, profile: Profile, cv_text: str, llm) -> FitReport | None:
    user = (
        f"CANDIDATE PROFILE\n{profile.headline}\nSeniority: {profile.seniority} | "
        f"Skills: {', '.join(profile.core_skills)}\n\nCV:\n{cv_text[:5000]}\n\n"
        f"JOB\n{job.get('title')} @ {job.get('company')}\n{(job.get('description') or '')[:4000]}"
    )
    try:
        data = json.loads(llm.complete(_SYSTEM, user, json_mode=True))
        score = max(0.0, min(1.0, float(data["confidence"])))
        loc_fit, loc_note = _location_fit(job, profile)   # location computed deterministically
        return FitReport(
            score=round(score, 3), coverage=round(score, 3),
            matched=list(data.get("matched", [])), missing=list(data.get("missing", [])),
            experience=str(data.get("experience", "")), summary=str(data.get("summary", "")),
            source="llm", location_fit=loc_fit, location_note=loc_note,
        )
    except Exception:  # noqa: BLE001 — fall back to heuristic on any failure
        return None


def assess_fit(job: dict, profile: Profile, cv_text: str, llm=None) -> FitReport:
    if llm is not None:
        report = llm_fit(job, profile, cv_text, llm)
        if report is not None:
            return report
    return heuristic_fit(job, profile, cv_text)
