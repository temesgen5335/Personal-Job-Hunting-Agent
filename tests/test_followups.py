"""Tier 2: follow-up reminders + drafted nudges.

Drafts only — there is deliberately no send path for follow-ups, and these tests
assert that no mailer is ever reachable from the draft flow.
"""

from datetime import datetime, timedelta, timezone

from jobagent.apply.generators import draft_followup, followup_prompt
from jobagent.core.schemas import ApplyMethod, Event, JobPosting, Source
from jobagent.digest import format_followups
from jobagent.store import Store


def _store(tmp_path):
    s = Store(str(tmp_path / "f.db"))
    s.init_schema()
    return s


def _submitted_app(store, *, days_ago: float, title="AI Engineer", company="Acme"):
    """Create a submitted application with a backdated submitted_at."""
    from jobagent.core.schemas import Application

    jid = store.upsert_job(JobPosting(
        source=Source.remoteok, title=title, company=company,
        apply_method=ApplyMethod.email, apply_email="jobs@acme.example",
    ))
    app_id = store.create_application(Application(job_id=jid, apply_method=ApplyMethod.email))
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    store.conn.execute(
        "UPDATE applications SET status='submitted', submitted_at=? WHERE id=?", (ts, app_id)
    )
    store.conn.commit()
    return app_id, jid


# --- the reminder query -----------------------------------------------------------

def test_quiet_application_surfaces_after_the_window(tmp_path):
    s = _store(tmp_path)
    _submitted_app(s, days_ago=10)
    pending = s.applications_needing_followup(after_days=7)
    assert len(pending) == 1
    assert pending[0]["days_waiting"] >= 10
    s.close()


def test_recent_application_does_not_surface(tmp_path):
    s = _store(tmp_path)
    _submitted_app(s, days_ago=2)
    assert s.applications_needing_followup(after_days=7) == []
    s.close()


def test_only_submitted_applications_are_chased(tmp_path):
    """Drafts, skipped, and closed-out applications are not waiting on anyone."""

    s = _store(tmp_path)
    app_id, _ = _submitted_app(s, days_ago=30)
    for status in ("rejected", "offer", "interview", "skipped", "awaiting_approval"):
        s.update_application(app_id, status=status)
        assert s.applications_needing_followup(after_days=7) == [], status
    s.update_application(app_id, status="submitted")
    assert len(s.applications_needing_followup(after_days=7)) == 1
    s.close()


def test_a_logged_followup_suppresses_the_reminder(tmp_path):
    s = _store(tmp_path)
    app_id, jid = _submitted_app(s, days_ago=10)
    s.log_event(Event(kind="followup_drafted", job_id=jid, payload={"application_id": app_id}))
    assert s.applications_needing_followup(after_days=7) == []
    s.close()


def test_reminder_renews_after_another_window(tmp_path):
    """One draft must not silence the reminder forever."""
    s = _store(tmp_path)
    app_id, jid = _submitted_app(s, days_ago=40)
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    s.conn.execute(
        "INSERT INTO events (kind, job_id, payload, created_at) VALUES ('followup_drafted',?,?,?)",
        (jid, f'{{"application_id": "{app_id}"}}', old),
    )
    s.conn.commit()
    # The nudge was 20 days ago, outside the 7-day window → it is due again.
    assert len(s.applications_needing_followup(after_days=7)) == 1
    s.close()


def test_a_followup_for_another_application_does_not_suppress(tmp_path):
    s = _store(tmp_path)
    app_a, _ = _submitted_app(s, days_ago=10, title="Role A", company="A Co")
    _submitted_app(s, days_ago=10, title="Role B", company="B Co")
    s.log_event(Event(kind="followup_drafted", payload={"application_id": app_a}))
    pending = s.applications_needing_followup(after_days=7)
    assert [p["title"] for p in pending] == ["Role B"]
    s.close()


def test_malformed_submitted_at_does_not_crash(tmp_path):
    s = _store(tmp_path)
    app_id, _ = _submitted_app(s, days_ago=10)
    s.conn.execute("UPDATE applications SET submitted_at='not-a-date' WHERE id=?", (app_id,))
    s.conn.commit()
    # Sorts/filters on the string still work; days_waiting degrades to the window.
    for row in s.applications_needing_followup(after_days=7):
        assert isinstance(row["days_waiting"], int)
    s.close()


# --- the drafted nudge ------------------------------------------------------------

class FakeLLM:
    def __init__(self, payload=None):
        self.payload = payload or '{"subject": "Following up on AI Engineer", "body": "Hello, ..."}'
        self.calls = []

    def complete(self, system, user, json_mode=False):
        self.calls.append((system, user, json_mode))
        return self.payload


def test_followup_prompt_forbids_fabrication_and_pressure():
    system, user = followup_prompt("Tester", {"title": "AI Engineer", "company": "Acme"}, 9)
    assert "Never invent" in system            # R1 carried through
    assert "Do not pressure" in system
    assert "imply a prior reply" in system     # no invented relationship
    assert "9" in user                         # the wait is given to the model


def test_draft_followup_parses_subject_and_body():
    llm = FakeLLM()
    subject, body = draft_followup("Tester", {"title": "AI Engineer"}, 9, llm)
    assert subject == "Following up on AI Engineer" and body.startswith("Hello")
    assert llm.calls[0][2] is True             # asked for JSON


def test_draft_followup_degrades_on_non_json():
    subject, body = draft_followup("T", {"title": "AI Engineer"}, 9, FakeLLM("just prose, no json"))
    assert subject == "Following up: AI Engineer"
    assert body == "just prose, no json"


def test_followup_generator_has_no_send_path():
    """Structural guarantee: nothing in the generators module can put mail on the wire,
    so a drafted nudge cannot become a sent one by accident."""
    import inspect

    import jobagent.apply.generators as gen
    src = inspect.getsource(gen)
    assert "smtplib" not in src
    assert "send_email" not in src
    assert "sendmail" not in src


# --- digest block -----------------------------------------------------------------

def test_digest_block_is_empty_when_nothing_is_waiting():
    assert format_followups([]) == ""           # no routine noise


def test_digest_block_lists_role_company_and_wait():
    out = format_followups([
        {"title": "AI Engineer", "company": "Acme", "days_waiting": 9},
        {"title": "Backend Engineer", "company": "Globex", "days_waiting": 14},
    ])
    assert "2 application(s) awaiting a reply" in out
    assert "AI Engineer" in out and "Acme" in out and "9d" in out
    assert "Globex" in out and "14d" in out


def test_followup_prompt_forbids_placeholders_and_dates():
    """A real run emitted '[date of application, 11 days ago]' into a sendable email."""
    system, _ = followup_prompt("T", {"title": "AI Engineer"}, 11)
    assert "no square brackets" in system and "no placeholders" in system
    assert "no specific dates" in system


def test_followup_prompt_forbids_claims_about_the_candidate():
    """No CV is supplied here, so the only safe posture is to claim nothing —
    a real run otherwise invented 'over 5 years of experience'."""
    system, _ = followup_prompt("T", {"title": "AI Engineer"}, 11)
    assert "MAKE NO CLAIMS ABOUT THE CANDIDATE" in system
    assert "years of experience" in system      # named explicitly as forbidden


def test_followup_multiline_json_is_parsed():
    class MultilineLLM:
        def complete(self, system, user, json_mode=False):
            return '{"subject": "Follow-up", "body": "Dear Team,\n\nFollowing up."}'

    subject, body = draft_followup("T", {"title": "AI Eng"}, 9, MultilineLLM())
    assert subject == "Follow-up" and body.startswith("Dear Team,")
    assert '"body"' not in body
