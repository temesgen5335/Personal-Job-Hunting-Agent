"""FastAPI orchestrator — the single backend the Telegram bot and Astro dashboard
both call (v2). Wraps the existing service layer (store, matching, apply, ats, llm).

SQLite is single-thread and FastAPI runs sync handlers in a threadpool, so every
handler opens its OWN Store and closes it. The app is created via create_app() so
tests can inject a temp store, a fake LLM, and a fake mailer.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from jobagent.apply import approve_and_send, prepare_application
from jobagent.apply.generators import draft_followup
from jobagent.apply.ats import apply_target
from jobagent.apply.ats_flow import create_ats_application, run_ats
from jobagent.apply.email_send import send_email
from jobagent.bot.service import MatchFilter, ranked_matches
from jobagent.config import get_settings, reload_settings
from jobagent.core.schemas import ApplicationStatus, Event, allowed_next, can_transition
from jobagent.fit import assess_fit
from jobagent.ingestion.gate import ALL_SOURCES, IngestGate, resolve_sources
from jobagent.ingestion.registry import build_adapters
from jobagent.ingestion.runner import run_ingestion
from jobagent.llm_client import build_llm
from jobagent.matching import run_matching
from jobagent.preferences import load_preferences
from jobagent.secrets_store import MANAGED_FIELDS, SecretStore, masked_view
from jobagent.store import Store

_UNSET = object()


def _decode_gaps(rows: list[dict]) -> list[dict]:
    """gaps is stored as JSON text; hand clients a real array so neither the
    dashboard nor any other consumer has to parse SQLite's encoding."""
    for r in rows:
        raw = r.get("gaps")
        if isinstance(raw, str):
            try:
                r["gaps"] = json.loads(raw or "[]")
            except (ValueError, TypeError):
                r["gaps"] = []
        elif raw is None:
            r["gaps"] = []
    return rows


class JobIdReq(BaseModel):
    job_id: str


class LoginReq(BaseModel):
    password: str


class ConfigPatch(BaseModel):
    values: dict


class FollowupReq(BaseModel):
    days_waiting: int | None = None


class TriageReq(BaseModel):
    action: str                     # dismiss | snooze | note | clear
    days: int = 3                   # snooze horizon
    note: str | None = None


class StatusReq(BaseModel):
    status: str
    # Escape hatch for fixing a mis-click. Bypasses the transition map and logs an
    # event, so an out-of-order change is possible but never silent.
    correction: bool = False


_VALID_STATUSES = {s.value for s in ApplicationStatus}


def _token_for(password: str, master_key: str) -> str:
    return hashlib.sha256(f"{password}|{master_key}".encode()).hexdigest()


def _ingest_task(db_path: str, settings, profile, llm, run_id: str) -> None:
    store = Store(db_path)
    try:
        # Same gate the scheduled pipeline uses — one seam, no drift.
        run_ingestion(build_adapters(settings), store, run_id=run_id,
                      gate=IngestGate.from_settings(get_settings()))
        run_matching(store, profile, llm=llm, run_id=run_id)
    finally:
        # The endpoint acquired the lock under this run_id before scheduling us.
        store.release_lock("pipeline", run_id)
        store.close()


def create_app(settings=None, profile=None, llm: Any = _UNSET, cv_master: str | None = None, mailer=None) -> FastAPI:
    settings = settings or get_settings()
    profile = profile or load_preferences().profile
    mailer = mailer or send_email
    # Injected llm (tests) is fixed; otherwise build fresh per call so config edits apply.
    llm_injected = llm is not _UNSET
    if cv_master is None:
        p = Path("config/cv_master.md")
        cv_master = p.read_text() if p.exists() else ""

    app = FastAPI(title="Personal Job Agent API", version="2.0")

    # The dashboard runs on a different origin and calls the API from the browser.
    from fastapi.middleware.cors import CORSMiddleware

    origins = [o.strip() for o in (settings.cors_origins or "*").split(",") if o.strip()] or ["*"]
    app.add_middleware(
        CORSMiddleware, allow_origins=origins,
        allow_methods=["*"], allow_headers=["*"],
    )

    def store() -> Store:
        return Store(settings.db_path)

    def _llm():
        return llm if llm_injected else build_llm(get_settings())

    # --- auth -----------------------------------------------------------------
    # Gates EVERY state-changing or cost-incurring route, not just /config. These
    # endpoints can send email as you, submit ATS forms, and spend LLM quota, so an
    # unauthenticated caller who can reach the port must not be able to drive them.
    # GETs stay open: they are read-only and the dashboard renders them server-side
    # without a token. tests/test_api.py asserts this gate covers every non-GET route.
    def _expected_token() -> str | None:
        return _token_for(settings.dashboard_password, settings.master_key) if settings.dashboard_password else None

    def require_auth(authorization: str | None = Header(None)) -> None:
        expected = _expected_token()
        if expected is None:
            # Fail closed: with no password there is no way to authenticate, so
            # refuse outright rather than leaving writes open.
            raise HTTPException(403, "Writes disabled — set DASHBOARD_PASSWORD to enable authenticated access.")
        token = (authorization or "").removeprefix("Bearer ").strip()
        if token != expected:
            raise HTTPException(401, "Unauthorized.")

    auth = [Depends(require_auth)]

    @app.post("/auth/login")
    def login(body: LoginReq):
        if not settings.dashboard_password:
            raise HTTPException(403, "Config UI disabled — set DASHBOARD_PASSWORD.")
        if body.password != settings.dashboard_password:
            raise HTTPException(401, "Wrong password.")
        return {"token": _token_for(body.password, settings.master_key)}

    def _effective_managed() -> dict:
        # env baseline (create_app's settings) overlaid by the encrypted store.
        base = {f: getattr(settings, f, None) for f in MANAGED_FIELDS}
        try:
            base.update({k: v for k, v in SecretStore().load().items() if k in MANAGED_FIELDS})
        except Exception:  # noqa: BLE001 — unreadable store → show env baseline only
            pass
        return base

    @app.get("/config", dependencies=auth)
    def get_config():
        return {"config": masked_view(_effective_managed())}

    @app.put("/config", dependencies=auth)
    def put_config(patch: ConfigPatch):
        try:
            SecretStore().update(patch.values)
        except RuntimeError as e:
            raise HTTPException(400, str(e))
        reload_settings()   # so other endpoints (build_llm) pick up new keys this process
        return {"config": masked_view(_effective_managed())}

    @app.get("/health")
    def health():
        chain = _llm()
        return {
            "status": "ok",
            "store_exists": Path(settings.db_path).exists(),
            "llm_chain": chain.chain if chain else [],
            "config_ui": bool(settings.dashboard_password),
        }

    @app.get("/stats")
    def stats():
        s = store()
        try:
            return s.stats()
        finally:
            s.close()

    @app.get("/jobs")
    def jobs(days: int = 0, location: str = "any", q: str | None = None,
             exclude: str | None = None, include: str | None = None,
             sources: str | None = None, limit: int = 50, offset: int = 0):
        split = lambda v: [x.strip() for x in (v or "").split(",") if x.strip()]  # noqa: E731
        flt = MatchFilter(
            max_age_days=days or None, location=location,
            keywords=[w for w in (q or "").replace(",", " ").split() if w],
            exclude_locations=split(exclude), include_locations=split(include),
            sources=split(sources),
            # The dashboard renders dismissed/snoozed rows with an Undo control, so
            # unlike every other consumer it wants them in the result set.
            hide_triaged=False,
        )
        s = store()
        try:
            return {"jobs": _decode_gaps(ranked_matches(s, limit, flt, offset=offset))}
        finally:
            s.close()

    @app.get("/applications")
    def applications(limit: int = 200):
        s = store()
        try:
            rows = s.list_applications(limit)
        finally:
            s.close()
        # allowed_next travels with each row so the UI never duplicates the
        # transition map — the Python definition stays the single source of truth.
        for r in rows:
            r["allowed_next"] = sorted(allowed_next(r["status"]))
        return {"applications": rows}

    @app.get("/job/{job_id}")
    def job_detail(job_id: str):
        s = store()
        try:
            job = s.get_job(job_id)
            if not job:
                raise HTTPException(404, "Job not found.")
            match = s.get_match(job_id) or {}
        finally:
            s.close()
        return _decode_gaps([{**job, **match}])[0]

    @app.patch("/applications/{app_id}", dependencies=auth)
    def update_application(app_id: str, body: StatusReq):
        if body.status not in _VALID_STATUSES:
            raise HTTPException(400, f"Invalid status. One of: {sorted(_VALID_STATUSES)}")
        s = store()
        try:
            existing = s.get_application(app_id)
            if not existing:
                raise HTTPException(404, "Application not found.")
            current = existing["status"]
            if not can_transition(current, body.status):
                if not body.correction:
                    # 422: the value is a real status, but the move is not part of the
                    # process. Name the legal moves so the caller can act on it.
                    raise HTTPException(422, {
                        "message": f"Cannot move {current} → {body.status}.",
                        "current": current,
                        "allowed": sorted(allowed_next(current)),
                        "hint": "Pass correction=true to override a mis-click (audited).",
                    })
                s.log_event(Event(kind="status_correction", job_id=existing.get("job_id"), payload={
                    "application_id": app_id, "from": current, "to": body.status,
                }))
            s.update_application(app_id, status=body.status)
        finally:
            s.close()
        return {"id": app_id, "status": body.status, "allowed_next": sorted(allowed_next(body.status))}

    @app.post("/triage/{job_id}", dependencies=auth)
    def triage(job_id: str, body: TriageReq):
        """One decision per job: dismiss (hide from the queue), snooze (hide for N
        days, lapses back on its own), note (annotate, stays live), clear (undo)."""
        from datetime import datetime, timedelta, timezone

        s = store()
        try:
            if not s.get_job(job_id):
                raise HTTPException(404, "Job not found.")
            if body.action == "dismiss":
                row = s.set_triage(job_id, state="dismissed", snoozed_until=None)
            elif body.action == "snooze":
                until = (datetime.now(timezone.utc) + timedelta(days=max(1, body.days))).isoformat()
                row = s.set_triage(job_id, state="snoozed", snoozed_until=until)
            elif body.action == "note":
                row = s.set_triage(job_id, note=body.note or "")
            elif body.action == "clear":
                s.clear_triage(job_id)
                row = s.get_triage(job_id) or {"job_id": job_id, "state": None, "note": None}
            else:
                raise HTTPException(400, "action must be dismiss | snooze | note | clear")
        finally:
            s.close()
        return {"job_id": job_id, "state": row.get("state"),
                "snoozed_until": row.get("snoozed_until"), "note": row.get("note")}

    @app.get("/followups")
    def followups(after_days: int = 7):
        """Submitted applications that have gone quiet. Read-only."""
        s = store()
        try:
            return {"followups": s.applications_needing_followup(after_days=after_days),
                    "after_days": after_days}
        finally:
            s.close()

    @app.post("/followups/{app_id}/draft", dependencies=auth)
    def followup_draft(app_id: str, body: FollowupReq | None = None):
        """Draft a nudge for a quiet application.

        DRAFT ONLY — there is deliberately no send endpoint for follow-ups. The user
        sends these personally, so nothing here can put mail on the wire.
        """
        current_llm = _llm()
        if current_llm is None:
            raise HTTPException(400, "No LLM configured (set an LLM key).")
        s = store()
        try:
            application = s.get_application(app_id)
            if not application:
                raise HTTPException(404, "Application not found.")
            job = s.get_job(application["job_id"])
            if not job:
                raise HTTPException(404, "Job not found.")
            days = (body.days_waiting if body and body.days_waiting is not None else 7)
            subject, text = draft_followup(profile.name or "", job, days, current_llm)
            # Logged so the reminder stops firing until the next window.
            s.log_event(Event(kind="followup_drafted", job_id=application["job_id"],
                              payload={"application_id": app_id, "days_waiting": days}))
        finally:
            s.close()
        return {"application_id": app_id, "subject": subject, "body": text,
                "to": job.get("apply_email"), "sent": False}

    @app.get("/analytics")
    def analytics():
        s = store()
        try:
            return s.application_analytics()
        finally:
            s.close()

    @app.post("/match", dependencies=auth)
    def match():
        s = store()
        try:
            r = run_matching(s, profile, llm=_llm())
            return {"scored": r.scored, "used_llm": r.used_llm, "llm_reranked": r.llm_reranked}
        finally:
            s.close()

    @app.post("/ingest", status_code=202, dependencies=auth)
    def ingest(bg: BackgroundTasks):
        # The id is returned immediately so the caller can watch /runs/{id} while
        # the background task is still going.
        run_id = uuid.uuid4().hex[:12]
        s = store()
        try:
            # Same lock the pipeline takes (M5) — acquired HERE, not in the task, so
            # the caller learns synchronously that a pass is already running.
            if not s.try_acquire_lock("pipeline", run_id):
                raise HTTPException(409, "An ingestion pass is already running.")
        finally:
            s.close()
        bg.add_task(_ingest_task, settings.db_path, settings, profile, _llm(), run_id)
        return {"status": "started", "run_id": run_id}

    @app.get("/sources")
    def sources_view():
        """Selectable ingest sources, which are enabled, and what is actually in the
        store — the dashboard needs all three: the full set for the Settings picker,
        the enabled set to preselect it, and the stored set for the Jobs visibility
        filter (offering a source with zero stored jobs is just noise)."""
        s = store()
        try:
            in_store = s.stats()["by_source"]
        finally:
            s.close()
        return {
            "available": ALL_SOURCES,
            "enabled": sorted(resolve_sources(get_settings(), load_preferences().sources)),
            "in_store": in_store,
        }

    @app.get("/runs")
    def runs(limit: int = 20):
        s = store()
        try:
            return {"runs": s.list_runs(limit)}
        finally:
            s.close()

    @app.get("/runs/{run_id}")
    def run_events(run_id: str):
        s = store()
        try:
            events = s.events_for_run(run_id)
        finally:
            s.close()
        if not events:
            raise HTTPException(404, "No events for that run id.")
        return {"run_id": run_id, "events": events}

    @app.post("/fit", dependencies=auth)
    def fit(req: JobIdReq):
        s = store()
        try:
            job = s.get_job(req.job_id)
            if not job:
                raise HTTPException(404, "Job not found.")
        finally:
            s.close()
        return assess_fit(job, profile, cv_master, _llm()).to_dict()

    @app.post("/apply/prepare", dependencies=auth)
    def apply_prepare(req: JobIdReq):
        current_llm = _llm()
        if current_llm is None:
            raise HTTPException(400, "No LLM configured (set an LLM key).")
        if not cv_master:
            raise HTTPException(400, "config/cv_master.md missing.")
        s = store()
        try:
            job = s.get_job(req.job_id)
            if not job:
                raise HTTPException(404, "Job not found.")
            b = prepare_application(s, job, profile, cv_master, current_llm)
            return {
                "application_id": b.application_id, "apply_method": b.apply_method,
                "cv_markdown": b.cv_markdown, "cover_letter": b.cover_letter,
                "email_subject": b.email_subject, "email_body": b.email_body,
            }
        finally:
            s.close()

    @app.post("/apply/{app_id}/approve", dependencies=auth)
    def apply_approve(app_id: str):
        s = store()
        try:
            return {"result": approve_and_send(s, app_id, settings, profile, mailer=mailer)}
        finally:
            s.close()

    @app.post("/ats/preview", dependencies=auth)
    def ats_preview(req: JobIdReq):
        s = store()
        try:
            job = s.get_job(req.job_id)
            if not job:
                raise HTTPException(404, "Job not found.")
            if apply_target(job)[0] is None:
                raise HTTPException(400, "Not a supported ATS (Greenhouse/Lever/Ashby).")
            app_id = create_ats_application(s, job)
            Path("artifacts").mkdir(exist_ok=True)
            shot = f"artifacts/ats_{app_id}.png"
            res = run_ats(s, app_id, profile, shot, submit=False)
        finally:
            s.close()
        return _ats_response(app_id, res)

    @app.post("/ats/{app_id}/submit", dependencies=auth)
    def ats_submit(app_id: str):
        s = store()
        try:
            Path("artifacts").mkdir(exist_ok=True)
            shot = f"artifacts/ats_{app_id}_submit.png"
            res = run_ats(s, app_id, profile, shot, submit=True)
        finally:
            s.close()
        return _ats_response(app_id, res)

    # The assistant lives in its own module: it is the only surface with a two-phase
    # confirmation flow, and keeping that out of here stops it being mistaken for the
    # ordinary single-request pattern every other route follows.
    from jobagent.api.assistant_routes import register as register_assistant

    # Held on app.state so the pending-approval registry is reachable — tests drive the
    # two-phase flow through it, and an operator surface can list what is waiting.
    app.state.assistant_pending = register_assistant(
        app, store_factory=store, settings_factory=get_settings, auth=auth)

    return app


def _ats_response(app_id: str, res) -> dict:
    return {
        "application_id": app_id, "platform": res.platform, "url": res.url,
        "filled": res.filled, "missing": res.missing,
        "captcha_detected": res.captcha_detected, "submitted": res.submitted,
        "screenshot_path": res.screenshot_path, "summary": res.summary(),
    }
