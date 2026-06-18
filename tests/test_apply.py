"""Phase 3 tests: generators (prompt rules), apply flow, HITL gate, email send.
No network, no LLM key — a FakeLLM and FakeSMTP stand in."""

import json

from jobagent.apply import approve_and_send, prepare_application
from jobagent.apply.email_send import build_message, send_email
from jobagent.apply.generators import cv_prompt, draft_email, email_prompt
from jobagent.core.schemas import ApplicationStatus, ApplyMethod, JobPosting, Source
from jobagent.preferences import Profile
from jobagent.store import Store

PROFILE = Profile(name="Temesgen G.", cv_path="docs/Temesgen_Gebreabzgi_CV.pdf")
CV_MASTER = "# CV\nPython, FastAPI, Next.js. AI Engineer at 10 Academy."


class FakeLLM:
    """Records calls; returns canned content (JSON for json_mode)."""
    def __init__(self):
        self.calls = []

    def complete(self, system, user, json_mode=False):
        self.calls.append((system, user, json_mode))
        if json_mode:
            return json.dumps({"subject": "Application: AI Engineer", "body": "Hello, please find attached."})
        return "TAILORED CONTENT"


class FakeSMTP:
    def __init__(self):
        self.sent = []
        self.logged_in = False
    def starttls(self, **kw):
        pass
    def login(self, u, p):
        self.logged_in = True
    def send_message(self, msg):
        self.sent.append(msg)
    def quit(self):
        pass


def _email_job(store) -> dict:
    jid = store.upsert_job(JobPosting(source=Source.telegram, title="AI Engineer", company="Acme",
                                      description="Build LLM systems.", is_remote=True,
                                      apply_method=ApplyMethod.email, apply_email="jobs@acme.example"))
    return store.get_job(jid)


def _ats_job(store) -> dict:
    jid = store.upsert_job(JobPosting(source=Source.greenhouse, title="Frontend Engineer",
                                      company="Globex", description="React.", apply_method=ApplyMethod.ats_form,
                                      apply_url="https://boards.greenhouse.io/globex/1"))
    return store.get_job(jid)


def test_cv_prompt_enforces_no_fabrication():
    system, user = cv_prompt(CV_MASTER, {"title": "AI Engineer", "description": "x"})
    assert "Never invent" in system
    assert CV_MASTER in user  # the real CV is the source of truth in the prompt


def test_draft_email_parses_json_then_falls_back():
    subj, body = draft_email("Temesgen", {"title": "AI Engineer"}, FakeLLM())
    assert subj == "Application: AI Engineer"

    class BadLLM:
        def complete(self, *a, **k):
            return "not json"
    subj2, body2 = draft_email("Temesgen", {"title": "AI Engineer"}, BadLLM())
    assert subj2 == "Application for AI Engineer"   # graceful fallback
    assert body2 == "not json"


def test_prepare_does_not_send_and_sets_awaiting_approval(tmp_path):
    store = Store(str(tmp_path / "a.db"))
    store.init_schema()
    job = _email_job(store)
    bundle = prepare_application(store, job, PROFILE, CV_MASTER, FakeLLM())

    app = store.get_application(bundle.application_id)
    assert app["status"] == ApplicationStatus.awaiting_approval.value
    assert app["approved_at"] is None        # R2: nothing approved yet
    assert app["submitted_at"] is None       # R2: nothing sent
    assert app["cv_variant_id"]              # CV variant persisted
    store.close()


def test_approve_sends_email_and_stamps_approval(tmp_path):
    store = Store(str(tmp_path / "a.db"))
    store.init_schema()
    job = _email_job(store)
    bundle = prepare_application(store, job, PROFILE, CV_MASTER, FakeLLM())

    smtp = FakeSMTP()

    class Settings:
        smtp_host = "smtp.test"; smtp_port = 587; smtp_user = "u"; smtp_password = "p"
        apply_from_email = "me@test.com"

    def mailer(settings, to, subject, body, attachment_path=None, smtp=smtp):
        send_email(settings, to, subject, body, attachment_path=None, smtp=smtp)

    msg = approve_and_send(store, bundle.application_id, Settings(), PROFILE, mailer=mailer)
    assert "Sent to jobs@acme.example" in msg
    assert len(smtp.sent) == 1
    app = store.get_application(bundle.application_id)
    assert app["status"] == ApplicationStatus.submitted.value
    assert app["approved_at"] is not None and app["submitted_at"] is not None
    store.close()


def test_ats_job_is_not_sent_deferred_to_phase4(tmp_path):
    store = Store(str(tmp_path / "a.db"))
    store.init_schema()
    job = _ats_job(store)
    bundle = prepare_application(store, job, PROFILE, CV_MASTER, FakeLLM())

    sent = []

    class Settings:
        smtp_host = "smtp.test"; smtp_port = 587; smtp_user = ""; smtp_password = ""
        apply_from_email = "me@test.com"

    msg = approve_and_send(store, bundle.application_id, Settings(), PROFILE,
                           mailer=lambda *a, **k: sent.append(1))
    assert "Phase 4" in msg
    assert sent == []                                   # R2: ATS form not auto-sent
    app = store.get_application(bundle.application_id)
    assert app["status"] == ApplicationStatus.awaiting_approval.value  # unchanged
    store.close()


def test_build_message_attaches_existing_file(tmp_path):
    f = tmp_path / "cv.pdf"
    f.write_bytes(b"%PDF-1.4 fake")
    msg = build_message("me@test.com", "to@test.com", "Subj", "Body", attachment_path=str(f))
    attachments = [p for p in msg.iter_attachments()]
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "cv.pdf"
