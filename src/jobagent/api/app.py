"""FastAPI orchestrator — the single backend the Telegram bot and Astro dashboard
both call (v2). Wraps the existing service layer (store, matching, apply, ats, llm).

SQLite is single-thread and FastAPI runs sync handlers in a threadpool, so every
handler opens its OWN Store and closes it. The app is created via create_app() so
tests can inject a temp store, a fake LLM, and a fake mailer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

from jobagent.apply import approve_and_send, prepare_application
from jobagent.apply.ats import apply_target
from jobagent.apply.ats_flow import create_ats_application, run_ats
from jobagent.apply.email_send import send_email
from jobagent.bot.service import MatchFilter, ranked_matches
from jobagent.config import get_settings
from jobagent.ingestion.registry import build_adapters
from jobagent.ingestion.runner import run_ingestion
from jobagent.llm_client import build_llm
from jobagent.matching import run_matching
from jobagent.preferences import load_preferences
from jobagent.store import Store

_UNSET = object()


class JobIdReq(BaseModel):
    job_id: str


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
    llm = build_llm(settings) if llm is _UNSET else llm
    mailer = mailer or send_email
    if cv_master is None:
        p = Path("config/cv_master.md")
        cv_master = p.read_text() if p.exists() else ""

    app = FastAPI(title="Personal Job Agent API", version="2.0")

    def store() -> Store:
        return Store(settings.db_path)

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "store_exists": Path(settings.db_path).exists(),
            "llm_chain": llm.chain if llm else [],
        }

    @app.get("/stats")
    def stats():
        s = store()
        try:
            return s.stats()
        finally:
            s.close()

    @app.get("/jobs")
    def jobs(days: int = 0, location: str = "any", q: str | None = None, limit: int = 50):
        flt = MatchFilter(
            max_age_days=days or None, location=location,
            keywords=[w for w in (q or "").replace(",", " ").split() if w],
        )
        s = store()
        try:
            return {"jobs": ranked_matches(s, limit, flt)}
        finally:
            s.close()

    @app.get("/applications")
    def applications(limit: int = 200):
        s = store()
        try:
            return {"applications": s.list_applications(limit)}
        finally:
            s.close()

    @app.post("/match")
    def match():
        s = store()
        try:
            r = run_matching(s, profile, llm=llm)
            return {"scored": r.scored, "used_llm": r.used_llm, "llm_reranked": r.llm_reranked}
        finally:
            s.close()

    @app.post("/ingest", status_code=202)
    def ingest(bg: BackgroundTasks):
        bg.add_task(_ingest_task, settings.db_path, settings, profile, llm)
        return {"status": "started"}

    @app.post("/apply/prepare")
    def apply_prepare(req: JobIdReq):
        if llm is None:
            raise HTTPException(400, "No LLM configured (set an LLM key).")
        if not cv_master:
            raise HTTPException(400, "config/cv_master.md missing.")
        s = store()
        try:
            job = s.get_job(req.job_id)
            if not job:
                raise HTTPException(404, "Job not found.")
            b = prepare_application(s, job, profile, cv_master, llm)
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
