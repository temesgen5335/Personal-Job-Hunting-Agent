"""Tier-1 application flow: prepare assets (no send) → HITL approval → send.

R2: `approve_and_send` is the ONLY function that transmits anything or stamps
`approved_at`. `prepare_application` generates and persists drafts in
status=awaiting_approval and sends nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from jobagent.apply.email_send import send_email
from jobagent.apply.generators import draft_email, tailor_cv, write_cover_letter
from jobagent.core.schemas import Application, ApplicationStatus, ApplyMethod, CVVariant, Event
from jobagent.preferences import Profile
from jobagent.store import Store

CV_MASTER_PATH = "config/cv_master.md"


def load_cv_master(path: str = CV_MASTER_PATH) -> str:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Master CV not found at {path} — needed to tailor applications")
    return p.read_text()


@dataclass
class AssetBundle:
    application_id: str
    job: dict
    cv_markdown: str
    cover_letter: str
    email_subject: str
    email_body: str
    apply_method: str


def prepare_application(store: Store, job: dict, profile: Profile, cv_master_md: str, llm) -> AssetBundle:
    """Generate tailored CV + cover letter + email draft; persist as awaiting_approval.
    Sends nothing."""
    cv_md = tailor_cv(cv_master_md, job, llm)
    cover = write_cover_letter(cv_master_md, job, llm)
    subject, body = draft_email(profile.name or "Candidate", job, llm)

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
    store.log_event(Event(kind="prepare", job_id=job["id"], payload={"application_id": app_id}))
    return AssetBundle(app_id, job, cv_md, cover, subject, body, job.get("apply_method") or "unknown")


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
