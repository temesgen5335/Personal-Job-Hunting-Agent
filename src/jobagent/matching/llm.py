"""Optional LLM reranker via OpenRouter (OpenAI-compatible API).

Used only on the top heuristic candidates (cost control). Gated on
OPENROUTER_API_KEY — without it the engine falls back to heuristic-only. The
`openai` package is an optional dep ([llm] extra), imported lazily.
"""

from __future__ import annotations

import json

from jobagent.preferences import Profile

_SYSTEM = (
    "You are a job-fit assessor for a specific candidate. Given the candidate "
    "profile and a job posting, return STRICT JSON: "
    '{"score": <0..1 float>, "rationale": "<one sentence>", '
    '"gaps": ["<missing requirement>", ...]}. '
    "Score reflects genuine fit for THIS candidate. Be honest about gaps. "
    "Do not invent candidate qualifications."
)


def _profile_blurb(p: Profile) -> str:
    return (
        f"Candidate: {p.headline}\n"
        f"Target roles: {', '.join(p.target_roles)}\n"
        f"Seniority: {p.seniority} | Work mode: {p.work_mode} | Location: {p.location} ({p.timezone})\n"
        f"Core skills: {', '.join(p.core_skills)}\n"
        f"Domains of interest: {', '.join(p.domains)}\n"
        f"Must-haves: {', '.join(p.must_haves)}"
    )


def llm_score(job: dict, profile: Profile, api_key: str, model: str) -> tuple[float, str, list[str]] | None:
    """Return (score, rationale, gaps) or None if the call/parse fails."""
    from openai import OpenAI  # lazy: optional [llm] extra

    client = OpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")
    user = (
        f"{_profile_blurb(profile)}\n\n"
        f"JOB\nTitle: {job.get('title')}\nCompany: {job.get('company')}\n"
        f"Location: {job.get('location')} | Remote: {bool(job.get('is_remote'))}\n"
        f"Description (truncated):\n{(job.get('description') or '')[:4000]}"
    )
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
            temperature=0,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        score = max(0.0, min(1.0, float(data["score"])))
        return round(score, 3), str(data.get("rationale", "")), list(data.get("gaps", []))
    except Exception:  # noqa: BLE001 — never let a scoring call break the run
        return None
