"""Generate application assets from the master CV + a job posting.

HARD RULE R1: tailoring REFRAMES real experience — it never invents skills, titles,
employers, dates, or metrics. The system prompts enforce this; keep it that way.

Each function splits a pure `*_prompt(...)` (testable) from the LLM call, and takes an
`llm` object exposing `.complete(system, user, json_mode=False)`.
"""

from __future__ import annotations

import json
import re

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
    "The email accompanies an attached CV and cover letter. "
    # The job description is in the prompt, and without this the model mirrors the
    # employer's requirements back as the candidate's qualifications. A real run
    # claimed "over 8 years of experience" and named three technologies absent from
    # the CV — in the email that actually gets sent. This is the R1 boundary.
    "CRITICAL: the job's requirements are NOT the candidate's background. Never state "
    "or imply that the candidate has a skill, technology, tool, or number of years "
    "unless it appears verbatim in the CV below. Do not echo the job's requirement list "
    "as the candidate's experience. If the CV does not evidence something, omit it. "
    "Prefer a short email that claims little over a longer one that overreaches. "
    'Return STRICT JSON: {"subject": "<concise subject>", "body": "<4-8 sentence email>"}.'
)


FOLLOWUP_SYSTEM = (
    "You write a brief, courteous follow-up email about a job application that has had "
    "no response. " + _NO_FABRICATION + " "
    # No CV is supplied to this prompt, so the model has nothing to ground a claim on.
    # Without this clause it invents them: a real run produced "over 5 years of
    # experience" for a candidate with three. A nudge does not need to re-sell — the
    # application already made the case — so forbid substantive claims outright.
    "MAKE NO CLAIMS ABOUT THE CANDIDATE. Do not state or imply years of experience, "
    "skills, technologies, seniority, achievements, or suitability. Do not restate the "
    "candidate's qualifications in any form. "
    "Two to three sentences, and only these moves: note that you applied, name the role, "
    "restate interest, offer to supply anything further, close politely. "
    "Never imply a prior reply, a referral, or a relationship that was not stated. "
    "Do not pressure, guilt, or set deadlines. "
    # A real run emitted "[date of application, 11 days ago]" — the wait is context for
    # tone, not content to quote, and a bracketed placeholder is worse than no date.
    "Output must be ready to send: no square brackets, no placeholders, no TODOs, and "
    "no specific dates. Refer to the wait only vaguely if at all. "
    'Return STRICT JSON: {"subject": "<concise subject>", "body": "<the email>"}.'
)


def _parse_subject_body(raw: str, fallback_subject: str) -> tuple[str, str]:
    """Parse a {"subject", "body"} JSON reply, tolerating real-world model output.

    `strict=False` is essential: models emit literal newlines inside JSON strings,
    which is invalid JSON. With strict parsing the whole raw blob fell through to the
    fallback and became the email body — sending `{"subject": ...}` to an employer.
    Markdown fences are stripped for the same reason.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        data = json.loads(text, strict=False)
        subject = str(data["subject"]).strip()
        body = str(data["body"]).strip()
        if not body:
            raise KeyError("body")
        return subject, body
    except (json.JSONDecodeError, KeyError, TypeError):
        # Last resort: hand back the prose. Never return JSON syntax as an email body.
        if text.lstrip().startswith("{"):
            match = re.search(r'"body"\s*:\s*"(.+?)"\s*}?\s*$', text, re.S)
            if match:
                return fallback_subject, match.group(1).replace("\\n", "\n").strip()
            return fallback_subject, ""
        return fallback_subject, text


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


def email_prompt(candidate_name: str, job: dict, cv_master_md: str = "") -> tuple[str, str]:
    """Build the application-email prompt.

    The CV is passed as the source of truth. Without it the only "facts" in the prompt
    are the employer's requirements, and the model asserts those as the candidate's —
    which is precisely the fabrication R1 forbids.
    """
    cv_block = (
        f"CANDIDATE CV (the ONLY permitted source of claims):\n{cv_master_md}\n\n"
        if cv_master_md else
        "NO CV WAS SUPPLIED. Make no claims about the candidate's background at all: "
        "state interest, name the role, and refer to the attached CV.\n\n"
    )
    return EMAIL_SYSTEM, f"Candidate name: {candidate_name}\n{cv_block}{_job_block(job)}"


def tailor_cv(cv_master_md: str, job: dict, llm) -> str:
    system, user = cv_prompt(cv_master_md, job)
    return llm.complete(system, user).strip()


def write_cover_letter(cv_master_md: str, job: dict, llm) -> str:
    system, user = cover_prompt(cv_master_md, job)
    return llm.complete(system, user).strip()


def draft_email(candidate_name: str, job: dict, llm, cv_master_md: str = "") -> tuple[str, str]:
    """Return (subject, body). Falls back gracefully if the model returns non-JSON.

    Pass cv_master_md wherever it is available — it is what keeps the email inside R1.
    """
    system, user = email_prompt(candidate_name, job, cv_master_md)
    raw = llm.complete(system, user, json_mode=True)
    return _parse_subject_body(raw, f"Application for {job.get('title')}")


# --- Drafter → reviewer → revise -------------------------------------------------
# A second pass that critiques a draft against the real CV and the job, then revises.
# Both prompts receive the CV (R1a) and re-assert the no-fabrication boundary (R1).
# These are NEW generator prompts: FakeLLM tests assert their guardrails, but per R1b
# only a live-model run proves them — which is why the flow gates the revise step
# behind a setting that ships OFF (see APPLY_REVIEW_ENABLED).

REVIEW_SYSTEM = (
    "You are a critical reviewer of a job-application draft (a CV or a cover letter). "
    "You do NOT rewrite it — you critique it. " + _NO_FABRICATION + " "
    "Judge the draft against the candidate's real CV and the job posting on: "
    "(1) fabrication risk — any claim not grounded in the CV; "
    "(2) missing keywords the CV genuinely supports but the draft omits; "
    "(3) weak or generic framing; (4) structure and length. "
    "NEVER suggest inventing experience to cover a gap — a genuine gap must stay visible. "
    'Return STRICT JSON: {"fabrication_risk": ["..."], "missing_keywords": ["..."], '
    '"weaknesses": ["..."], "verdict": "ok" | "revise"}.'
)

REVISE_SYSTEM = (
    "You revise a job-application draft using a reviewer's critique. " + _NO_FABRICATION + " "
    "Apply ONLY changes grounded in the candidate's CV: add a missing keyword ONLY if the "
    "CV genuinely supports it, tighten weak framing, fix structure and length. Leave "
    "genuine gaps visible — never invent experience to satisfy a critique, and never stuff "
    "keywords the CV does not support. Preserve the draft's format exactly (a Markdown CV "
    "stays Markdown; a cover letter stays prose). Output only the revised draft, no commentary."
)


def review_prompt(kind: str, draft: str, cv_master_md: str, job: dict) -> tuple[str, str]:
    return REVIEW_SYSTEM, (
        f"DRAFT KIND: {kind}\n"
        f"CANDIDATE CV (source of truth):\n{cv_master_md}\n\n"
        f"{_job_block(job)}\n\nDRAFT TO REVIEW:\n{draft}"
    )


def _parse_review(raw: str) -> dict:
    """Parse the reviewer JSON, degrading to a neutral 'ok' verdict on bad output so a
    malformed critique never blocks or corrupts the pipeline."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        data = json.loads(text, strict=False)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"fabrication_risk": [], "missing_keywords": [], "weaknesses": [], "verdict": "ok"}
    listy = lambda k: [str(x) for x in data.get(k) or [] if str(x).strip()]  # noqa: E731
    verdict = "revise" if str(data.get("verdict", "")).lower() == "revise" else "ok"
    return {
        "fabrication_risk": listy("fabrication_risk"),
        "missing_keywords": listy("missing_keywords"),
        "weaknesses": listy("weaknesses"),
        "verdict": verdict,
    }


def review_draft(kind: str, draft: str, cv_master_md: str, job: dict, llm) -> dict:
    """Critique a draft. Returns the parsed reviewer verdict (never raises on bad JSON)."""
    system, user = review_prompt(kind, draft, cv_master_md, job)
    return _parse_review(llm.complete(system, user, json_mode=True))


def revise_prompt(kind: str, draft: str, critique: dict, cv_master_md: str, job: dict) -> tuple[str, str]:
    return REVISE_SYSTEM, (
        f"DRAFT KIND: {kind}\n"
        f"CANDIDATE CV (the ONLY permitted source of claims):\n{cv_master_md}\n\n"
        f"{_job_block(job)}\n\n"
        f"REVIEWER CRITIQUE (JSON):\n{json.dumps(critique)}\n\n"
        f"CURRENT DRAFT:\n{draft}"
    )


def revise_draft(kind: str, draft: str, critique: dict, cv_master_md: str, job: dict, llm) -> str:
    system, user = revise_prompt(kind, draft, critique, cv_master_md, job)
    return llm.complete(system, user).strip()


def followup_prompt(candidate_name: str, job: dict, days_waiting: int) -> tuple[str, str]:
    return FOLLOWUP_SYSTEM, (
        f"Candidate name: {candidate_name}\n"
        f"Days since the application was submitted: {days_waiting}\n"
        f"{_job_block(job)}"
    )


def draft_followup(candidate_name: str, job: dict, days_waiting: int, llm) -> tuple[str, str]:
    """Draft a follow-up nudge. Returns (subject, body).

    DRAFT ONLY — there is deliberately no send path for follow-ups. The user sends
    these personally, so nothing here can put mail on the wire (R2 in spirit: no
    outbound message without an explicit human action).
    """
    system, user = followup_prompt(candidate_name, job, days_waiting)
    raw = llm.complete(system, user, json_mode=True)
    return _parse_subject_body(raw, f"Following up: {job.get('title')}")
