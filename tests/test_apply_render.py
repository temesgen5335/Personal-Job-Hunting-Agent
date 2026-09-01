"""Markdown→PDF CV render, and the render→verify→attach loop it closes.

Offline (R17): fpdf2 is pure Python (no network, no subprocess), so the happy-path tests
use importorskip; the degradation and attachment-selection tests need no PDF library at
all. The point of the feature is that the report verifies the same file that gets
attached — these tests pin exactly that."""

import json
from pathlib import Path

import pytest

from jobagent.apply import approve_and_send, prepare_application
from jobagent.apply.flow import cv_pdf_path_for
from jobagent.apply.render import RenderUnavailable, is_available, render_cv_pdf
from jobagent.apply.verify import ats_report_for_pdf
from jobagent.core.schemas import ApplyMethod, JobPosting, Source
from jobagent.preferences import Profile
from jobagent.store import Store

JOB = {"id": "j1", "title": "Senior Python Engineer", "company": "Acme", "location": "Remote",
       "description": "Python, FastAPI, PostgreSQL. Build LLM data pipelines."}
CV = ("# Ada Lovelace\nada@example.com | +251 900 111 222\n\n"
      "## Experience\n- Built LLM systems with Python and FastAPI\n- Data pipelines on PostgreSQL")
PROFILE = Profile(name="Ada Lovelace", email="ada@example.com", phone="+251900111222",
                  cv_path="static_cv.pdf")


class FakeLLM:
    def complete(self, system, user, json_mode=False):
        return json.dumps({"subject": "Application", "body": "Hello."}) if json_mode else CV


class RenderOn:
    apply_review_enabled = False
    apply_review_rounds = 1
    apply_render_cv_pdf = True


def _email_job(store) -> dict:
    jid = store.upsert_job(JobPosting(source=Source.telegram, title="Senior Python Engineer",
                                      company="Acme", description=JOB["description"], is_remote=True,
                                      apply_method=ApplyMethod.email, apply_email="jobs@acme.example"))
    return store.get_job(jid)


def _capture_mailer(captured):
    def mailer(settings, to, subject, body, attachment_path=None):
        captured["attachment"] = attachment_path
    return mailer


# --- the renderer itself (needs fpdf2 + pypdf) ----------------------------------

def test_rendered_pdf_has_an_extractable_text_layer(tmp_path):
    pytest.importorskip("fpdf")
    pytest.importorskip("pypdf")
    out = str(tmp_path / "cv.pdf")
    render_cv_pdf(CV, out)
    r = ats_report_for_pdf(out, JOB, name="Ada Lovelace",
                           email="ada@example.com", phone="+251900111222")
    assert r.extractor in ("pypdf", "pdftotext")
    assert r.ok                              # contact details survived to the text layer
    assert "python" in r.covered


def test_render_unavailable_raises_clearly():
    if is_available():
        pytest.skip("fpdf2 installed; the unavailable path cannot be exercised here")
    with pytest.raises(RenderUnavailable):
        render_cv_pdf(CV, "unused.pdf")


# --- the flow: render off by default --------------------------------------------

def test_prepare_without_render_flag_verifies_markdown(tmp_path):
    store = Store(str(tmp_path / "a.db"))
    store.init_schema()
    bundle = prepare_application(store, _email_job(store), PROFILE, CV, FakeLLM())  # no settings
    assert bundle.cv_pdf_path is None
    assert bundle.ats.extractor == "markdown"
    store.close()


# --- the flow: render on but renderer fails → graceful degradation ---------------

def test_prepare_degrades_to_markdown_when_render_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def boom(*a, **k):
        raise RenderUnavailable("no fpdf")
    monkeypatch.setattr("jobagent.apply.render.render_cv_pdf", boom)

    store = Store(str(tmp_path / "a.db"))
    store.init_schema()
    bundle = prepare_application(store, _email_job(store), PROFILE, CV, FakeLLM(), settings=RenderOn())
    assert bundle.cv_pdf_path is None            # degraded, did not crash
    assert bundle.ats.extractor == "markdown"    # verified the Markdown instead
    store.close()


# --- the flow: render on with a real renderer → PDF made and verified ------------

def test_prepare_renders_and_verifies_the_same_pdf(tmp_path, monkeypatch):
    pytest.importorskip("fpdf")
    pytest.importorskip("pypdf")
    monkeypatch.chdir(tmp_path)
    store = Store(str(tmp_path / "a.db"))
    store.init_schema()
    bundle = prepare_application(store, _email_job(store), PROFILE, CV, FakeLLM(), settings=RenderOn())
    assert bundle.cv_pdf_path is not None
    assert Path(bundle.cv_pdf_path).is_file()
    assert bundle.ats.extractor in ("pypdf", "pdftotext")   # verified the PDF, not the md
    # the verified file is exactly the one approve will attach
    assert Path(bundle.cv_pdf_path) == cv_pdf_path_for(bundle.application_id)
    store.close()


# --- approve attaches the rendered PDF when present, else the static CV -----------

def test_approve_attaches_rendered_pdf_when_present(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = Store(str(tmp_path / "a.db"))
    store.init_schema()
    bundle = prepare_application(store, _email_job(store), PROFILE, CV, FakeLLM())  # render off
    pdf = cv_pdf_path_for(bundle.application_id)
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4 fake")            # stand in for a rendered CV
    captured = {}
    approve_and_send(store, bundle.application_id, object(), PROFILE,
                     mailer=_capture_mailer(captured))
    assert captured["attachment"] == str(pdf)
    store.close()


def test_approve_falls_back_to_static_cv_when_no_pdf(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = Store(str(tmp_path / "a.db"))
    store.init_schema()
    bundle = prepare_application(store, _email_job(store), PROFILE, CV, FakeLLM())
    captured = {}
    approve_and_send(store, bundle.application_id, object(), PROFILE,
                     mailer=_capture_mailer(captured))
    assert captured["attachment"] == "static_cv.pdf"
    store.close()
