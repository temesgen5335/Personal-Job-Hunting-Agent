"""Generate application assets from the master CV + a job posting.

HARD RULE R1: tailoring REFRAMES real experience — it never invents skills, titles,
employers, dates, or metrics. The system prompts enforce this; keep it that way.

Each function splits a pure `*_prompt(...)` (testable) from the LLM call, and takes an
`llm` object exposing `.complete(system, user, json_mode=False)`.
"""

from __future__ import annotations

import json

_NO_FABRICATION = (
    "ABSOLUTE RULE: Use ONLY facts present in the candidate's CV. Never invent or "
    "exaggerate skills, employers, titles, dates, degrees, or metrics. You may "
    "reorder, re-emphasize, and rephrase real content to fit the job — nothing more. "
    "If the candidate lacks a requirement, do not claim it."
)

CV_SYSTEM = (
    "You tailor a candidate's CV to a specific job. " + _NO_FABRICATION + " "
    "Output the tailored CV in clean Markdown: reorder and emphasize the most relevant "
    "experience, projects, and skills first; tighten the summary to the role. Keep it "
    "truthful and ATS-friendly. Output only the CV Markdown, no commentary."
)

COVER_SYSTEM = (
    "You write a concise, professional cover letter (250-350 words). " + _NO_FABRICATION
    + " Ground every claim in the candidate's real experience. No clichés or filler. "
    "Output only the letter text."
)

EMAIL_SYSTEM = (
    "You write a short, professional job-application email. " + _NO_FABRICATION + " "
    "The email accompanies an attached CV and cover letter. Return STRICT JSON: "
    '{"subject": "<concise subject>", "body": "<4-8 sentence email>"}.'
)


def _job_block(job: dict) -> str:
    return (
        f"JOB\nTitle: {job.get('title')}\nCompany: {job.get('company')}\n"
        f"Location: {job.get('location')}\n"
        f"Description:\n{(job.get('description') or '')[:6000]}"
    )


def cv_prompt(cv_master_md: str, job: dict) -> tuple[str, str]:
    return CV_SYSTEM, f"CANDIDATE CV (source of truth):\n{cv_master_md}\n\n{_job_block(job)}"


def cover_prompt(cv_master_md: str, job: dict) -> tuple[str, str]:
    return COVER_SYSTEM, f"CANDIDATE CV:\n{cv_master_md}\n\n{_job_block(job)}"


def email_prompt(candidate_name: str, job: dict) -> tuple[str, str]:
    return EMAIL_SYSTEM, f"Candidate name: {candidate_name}\n{_job_block(job)}"


def tailor_cv(cv_master_md: str, job: dict, llm) -> str:
    system, user = cv_prompt(cv_master_md, job)
    return llm.complete(system, user).strip()


def write_cover_letter(cv_master_md: str, job: dict, llm) -> str:
    system, user = cover_prompt(cv_master_md, job)
    return llm.complete(system, user).strip()


def draft_email(candidate_name: str, job: dict, llm) -> tuple[str, str]:
    """Return (subject, body). Falls back gracefully if the model returns non-JSON."""
    system, user = email_prompt(candidate_name, job)
    raw = llm.complete(system, user, json_mode=True)
    try:
        data = json.loads(raw)
        return str(data["subject"]).strip(), str(data["body"]).strip()
    except (json.JSONDecodeError, KeyError, TypeError):
        return f"Application for {job.get('title')}", raw.strip()
