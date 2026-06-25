"""FastAPI orchestrator — the single backend the Telegram bot and Astro dashboard
both call (v2). Wraps the existing service layer (store, matching, apply, ats, llm).

SQLite is single-thread and FastAPI runs sync handlers in a threadpool, so every
handler opens its OWN Store and closes it. The app is created via create_app() so
tests can inject a temp store, a fake LLM, and a fake mailer.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from jobagent.apply import approve_and_send, prepare_application
from jobagent.apply.ats import apply_target
from jobagent.apply.ats_flow import create_ats_application, run_ats
from jobagent.apply.email_send import send_email
from jobagent.bot.service import MatchFilter, ranked_matches
from jobagent.config import get_settings, reload_settings
from jobagent.core.schemas import ApplicationStatus
from jobagent.fit import assess_fit
from jobagent.ingestion.registry import build_adapters
from jobagent.ingestion.runner import run_ingestion
from jobagent.llm_client import build_llm
from jobagent.matching import run_matching
from jobagent.preferences import load_preferences
from jobagent.secrets_store import MANAGED_FIELDS, SecretStore, masked_view
from jobagent.store import Store

_UNSET = object()


class JobIdReq(BaseModel):
    job_id: str


class LoginReq(BaseModel):
    password: str


class ConfigPatch(BaseModel):
    values: dict


class StatusReq(BaseModel):
    status: str


_VALID_STATUSES = {s.value for s in ApplicationStatus}


def _token_for(password: str, master_key: str) -> str:
    return hashlib.sha256(f"{password}|{master_key}".encode()).hexdigest()


def _ingest_task(db_path: str, settings, profile, llm) -> None:
    store = Store(db_path)
    try:
        run_ingestion(build_adapters(settings), store)
        run_matching(store, profile, llm=llm)
    finally:
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

    # --- auth (gates /config) -------------------------------------------------
    def _expected_token() -> str | None:
        return _token_for(settings.dashboard_password, settings.master_key) if settings.dashboard_password else None

    def require_auth(authorization: str | None = Header(None)) -> None:
        expected = _expected_token()
        if expected is None:
            raise HTTPException(403, "Config UI disabled — set DASHBOARD_PASSWORD.")
        token = (authorization or "").removeprefix("Bearer ").strip()
        if token != expected:
            raise HTTPException(401, "Unauthorized.")

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

    @app.get("/config", dependencies=[Depends(require_auth)])
    def get_config():
        return {"config": masked_view(_effective_managed())}

    @app.put("/config", dependencies=[Depends(require_auth)])
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
             limit: int = 50, offset: int = 0):
        split = lambda v: [x.strip() for x in (v or "").split(",") if x.strip()]  # noqa: E731
        flt = MatchFilter(
            max_age_days=days or None, location=location,
            keywords=[w for w in (q or "").replace(",", " ").split() if w],
            exclude_locations=split(exclude), include_locations=split(include),
        )
        s = store()
        try:
            return {"jobs": ranked_matches(s, limit, flt, offset=offset)}
        finally:
            s.close()

    @app.get("/applications")
    def applications(limit: int = 200):
        s = store()
        try:
            return {"applications": s.list_applications(limit)}
        finally:
            s.close()

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
        return {**job, **match}

    @app.patch("/applications/{app_id}")
    def update_application(app_id: str, body: StatusReq):
        if body.status not in _VALID_STATUSES:
            raise HTTPException(400, f"Invalid status. One of: {sorted(_VALID_STATUSES)}")
        s = store()
        try:
            if not s.get_application(app_id):
                raise HTTPException(404, "Application not found.")
            s.update_application(app_id, status=body.status)
        finally:
            s.close()
        return {"id": app_id, "status": body.status}

    @app.get("/analytics")
    def analytics():
        s = store()
        try:
            return s.application_analytics()
        finally:
            s.close()

    @app.post("/match")
    def match():
        s = store()
        try:
            r = run_matching(s, profile, llm=_llm())
            return {"scored": r.scored, "used_llm": r.used_llm, "llm_reranked": r.llm_reranked}
        finally:
            s.close()

    @app.post("/ingest", status_code=202)
    def ingest(bg: BackgroundTasks):
        bg.add_task(_ingest_task, settings.db_path, settings, profile, _llm())
        return {"status": "started"}

    @app.post("/fit")
    def fit(req: JobIdReq):
        s = store()
        try:
            job = s.get_job(req.job_id)
            if not job:
                raise HTTPException(404, "Job not found.")
        finally:
            s.close()
        return assess_fit(job, profile, cv_master, _llm()).to_dict()

    @app.post("/apply/prepare")
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

    @app.post("/apply/{app_id}/approve")
    def apply_approve(app_id: str):
        s = store()
        try:
            return {"result": approve_and_send(s, app_id, settings, profile, mailer=mailer)}
        finally:
            s.close()

    @app.post("/ats/preview")
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

    @app.post("/ats/{app_id}/submit")
    def ats_submit(app_id: str):
        s = store()
        try:
            Path("artifacts").mkdir(exist_ok=True)
            shot = f"artifacts/ats_{app_id}_submit.png"
            res = run_ats(s, app_id, profile, shot, submit=True)
        finally:
            s.close()
        return _ats_response(app_id, res)

    return app


def _ats_response(app_id: str, res) -> dict:
    return {
        "application_id": app_id, "platform": res.platform, "url": res.url,
        "filled": res.filled, "missing": res.missing,
        "captcha_detected": res.captcha_detected, "submitted": res.submitted,
        "screenshot_path": res.screenshot_path, "summary": res.summary(),
    }
