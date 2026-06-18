"""Tier-2 (ATS form-fill) flow + HITL gate, shared by the CLI and the Telegram bot.

R2: submission happens only via run_ats(submit=True), which is reached only on an
explicit user approval action. R3: a CAPTCHA aborts submission (handled in executor).
"""

from __future__ import annotations

from datetime import datetime, timezone

from jobagent.apply.ats import ApplicantInfo, ExecResult, apply_target, apply_to_job
from jobagent.core.schemas import Application, ApplicationStatus, ApplyMethod, Event
from jobagent.preferences import Profile
from jobagent.store import Store


def create_ats_application(store: Store, job: dict) -> str:
    """Persist a bare ATS application (awaiting_approval). No LLM, no send."""
    app_id = store.create_application(
        Application(job_id=job["id"], status=ApplicationStatus.awaiting_approval,
                    apply_method=ApplyMethod.ats_form)
    )
    store.log_event(Event(kind="prepare_ats", job_id=job["id"], payload={"application_id": app_id}))
    return app_id


def run_ats(store: Store, application_id: str, profile: Profile, screenshot_path: str,
            submit: bool = False) -> ExecResult:
    """Fill (and optionally submit) the ATS form for an application. Raises ValueError
    if the job isn't a supported ATS."""
    app = store.get_application(application_id)
    if not app:
        raise ValueError(f"Application {application_id} not found.")
    job = store.get_job(app["job_id"])
    platform, url = apply_target(job or {})
    if platform is None:
        raise ValueError(f"Unsupported ATS for: {url}")

    result = apply_to_job(platform, url, ApplicantInfo.from_profile(profile), screenshot_path, submit=submit)

    if submit and result.submitted:
        now = datetime.now(timezone.utc).isoformat()
        store.update_application(application_id, status=ApplicationStatus.submitted.value,
                                 approved_at=now, submitted_at=now)
        store.log_event(Event(kind="submit_ats", job_id=job["id"],
                              payload={"platform": platform, "url": url}))
    return result
