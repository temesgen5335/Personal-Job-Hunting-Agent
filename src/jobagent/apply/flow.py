"""Tier-1 application flow: prepare assets (no send) → HITL approval → send.

R2: `approve_and_send` is the ONLY function that transmits anything or stamps
`approved_at`. `prepare_application` generates and persists drafts in
status=awaiting_approval and sends nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from jobagent.apply.email_send import send_email
from jobagent.apply.generators import (
    draft_email, review_draft, revise_draft, tailor_cv, write_cover_letter,
)
from jobagent.apply.verify import AtsReport, ats_report
from jobagent.core.schemas import Application, ApplicationStatus, ApplyMethod, CVVariant, Event
from jobagent.preferences import Profile
from jobagent.store import Store

CV_MASTER_PATH = "config/cv_master.md"


def load_cv_master(path: str | None = None) -> str:
    """The master CV. Delegates to the preferences layer so there is one source of
    truth — the writable `data/cv_master.md` wins over the legacy `config/` copy.
    Still raises if none exists, since an application cannot be tailored without it."""
    from jobagent.preferences import load_cv_master as _load

    text = _load(path)
    if not text:
        raise FileNotFoundError(
            "Master CV not found (data/cv_master.md or config/cv_master.md) — "
            "needed to tailor applications. Add it in Settings → Profile.")
    return text


@dataclass
class AssetBundle:
    application_id: str
    job: dict
    cv_markdown: str
    cover_letter: str
    email_subject: str
    email_body: str
    apply_method: str
    ats: AtsReport | None = None      # ATS-parseability report on the tailored CV
    review: dict | None = None        # reviewer verdicts, only when review is enabled


def _review_and_revise(kind: str, draft: str, cv_master_md: str, job: dict, llm, rounds: int) -> tuple[str, dict]:
    """Critique the draft and, if the reviewer says so, revise it — up to `rounds` times.
    Returns the (possibly revised) draft and the last critique."""
    critique: dict = {"verdict": "ok"}
    for _ in range(max(1, rounds)):
        critique = review_draft(kind, draft, cv_master_md, job, llm)
        if critique.get("verdict") != "revise":
            break
        draft = revise_draft(kind, draft, critique, cv_master_md, job, llm)
    return draft, critique


def prepare_application(store: Store, job: dict, profile: Profile, cv_master_md: str,
                        llm, settings=None) -> AssetBundle:
    """Generate tailored CV + cover letter + email draft; persist as awaiting_approval.
    Sends nothing.

    An ATS-parseability report on the CV is always attached (read-only; it never edits
    content). The drafter→reviewer→revise loop runs only when `settings.apply_review_enabled`
    is set — it rewrites the draft, so it ships OFF and needs a live-model check first (R1b).
    """
    cv_md = tailor_cv(cv_master_md, job, llm)
    cover = write_cover_letter(cv_master_md, job, llm)

    review: dict | None = None
    if settings is not None and getattr(settings, "apply_review_enabled", False):
        rounds = getattr(settings, "apply_review_rounds", 1)
        cv_md, cv_crit = _review_and_revise("cv", cv_md, cv_master_md, job, llm, rounds)
        cover, cover_crit = _review_and_revise("cover_letter", cover, cv_master_md, job, llm, rounds)
        review = {"cv": cv_crit, "cover_letter": cover_crit}

    subject, body = draft_email(profile.name or "Candidate", job, llm, cv_master_md)

    ats = ats_report(cv_md, job, name=profile.name, email=profile.email, phone=profile.phone)

    cv_id = store.insert_cv_variant(
        CVVariant(job_id=job["id"], base_cv_id="master",
                  content_markdown=cv_md, notes=f"Tailored to {job.get('company')} — {job.get('title')}")
    )
    app_id = store.create_application(
        Application(
            job_id=job["id"],
            status=ApplicationStatus.awaiting_approval,
            cv_variant_id=cv_id,
            cover_letter=cover,
            email_draft=json.dumps({"subject": subject, "body": body}),
            apply_method=ApplyMethod(job.get("apply_method") or "unknown"),
        )
    )
    store.log_event(Event(kind="prepare", job_id=job["id"], payload={
        "application_id": app_id,
        "ats_ok": ats.ok, "ats_coverage": round(ats.coverage, 3),
        "ats_missing": ats.missing[:8], "reviewed": review is not None,
    }))
    return AssetBundle(app_id, job, cv_md, cover, subject, body,
                       job.get("apply_method") or "unknown", ats=ats, review=review)


def approve_and_send(store: Store, application_id: str, settings, profile: Profile, mailer=send_email) -> str:
    """HITL gate. Only call on explicit user approval. Sends Tier-1 (email) apps and
    stamps approval; hands Tier-2 (ATS form) apps to Phase 4 without sending."""
    app = store.get_application(application_id)
    if not app:
        return f"Application {application_id} not found."
    if app["status"] == ApplicationStatus.submitted.value:
        return "Already submitted."

    job = store.get_job(app["job_id"])
    method = app["apply_method"]

    if method != ApplyMethod.email.value:
        return (
            f"This is a *{method}* application — Tier-2 HITL form-fill is Phase 4. "
            f"Apply manually for now: {job.get('apply_url') or job.get('url')}"
        )

    to_addr = (job or {}).get("apply_email")
    if not to_addr:
        return "No application email on this posting; cannot send."

    draft = json.loads(app["email_draft"])
    now = datetime.now(timezone.utc).isoformat()
    mailer(
        settings, to_addr, draft["subject"], draft["body"],
        attachment_path=profile.cv_path or None,
    )
    store.update_application(application_id, status=ApplicationStatus.submitted.value,
                             approved_at=now, submitted_at=now)
    store.log_event(Event(kind="submit", job_id=app["job_id"],
                          payload={"to": to_addr, "subject": draft["subject"]}))
    return f"✅ Sent to {to_addr} — “{draft['subject']}”."
