"""Tier-1 send path: the HITL gate. `approve_and_send` is the ONLY function here that
transmits anything or stamps `approved_at`.

Draft preparation moved to `jobagent.apply.prepare` so the agent's draft tool can import
it without a mailer in the graph (R2). The four draft names are re-exported here so every
existing `from jobagent.apply.flow import ...` and `flow.prepare_application` keeps working.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from jobagent.apply.email_send import send_email
from jobagent.apply.prepare import (  # re-export: preserve the old import paths
    AssetBundle, CV_MASTER_PATH, cv_pdf_path_for, load_cv_master, prepare_application,
)
from jobagent.core.schemas import ApplicationStatus, ApplyMethod, Event
from jobagent.preferences import Profile
from jobagent.store import Store

__all__ = [
    "AssetBundle", "CV_MASTER_PATH", "cv_pdf_path_for", "load_cv_master",
    "prepare_application", "approve_and_send",
]


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
    # Attach the tailored CV PDF if one was rendered at prepare time (the artifact the
    # ATS report verified); otherwise fall back to the candidate's static CV.
    rendered = cv_pdf_path_for(application_id)
    attachment = str(rendered) if rendered.is_file() else (profile.cv_path or None)
    mailer(
        settings, to_addr, draft["subject"], draft["body"],
        attachment_path=attachment,
    )
    store.update_application(application_id, status=ApplicationStatus.submitted.value,
                             approved_at=now, submitted_at=now)
    store.log_event(Event(kind="submit", job_id=app["job_id"],
                          payload={"to": to_addr, "subject": draft["subject"],
                                   "cv": "tailored_pdf" if rendered.is_file() else "static"}))
    return f"✅ Sent to {to_addr} — “{draft['subject']}”."
