"""Inbox outcome detection.

The whole feature is a proposal mechanism, so most of these tests are about what it
must NOT do: never apply a change on its own, never attribute a reply to the wrong
application, and never bypass the transition map. A wrong automatic outcome corrupts
the operator's own history silently, which is worse than detecting nothing.

No IMAP, no network: the connection is a fake (R17).
"""

import email
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from jobagent.api import create_app
from jobagent.config import Settings
from jobagent.core.schemas import Application, ApplyMethod, JobPosting, Source
from jobagent.inbox.classify import classify
from jobagent.inbox.reader import (
    InboxReader,
    body_text,
    decode_field,
    match_application,
    scan,
)
from jobagent.preferences import Profile
from jobagent.store import Store


# --- the classifier -----------------------------------------------------------

@pytest.mark.parametrize("subject,body,expected", [
    ("Update on your application",
     "Unfortunately we have decided not to move forward at this time.", "rejected"),
    ("Your application", "The position has been filled.", "rejected"),
    ("Next steps",
     "We would love to schedule a call. Please share your availability for the interview.",
     "interview"),
    ("Moving forward", "We'd like to move you to the next stage.", "interview"),
    ("Great news", "We are pleased to offer you the position. A formal offer follows.",
     "offer"),
])
def test_it_reads_real_reply_shapes(subject, body, expected):
    assert classify(subject, body).status == expected


def test_a_rejection_that_mentions_future_interviews_is_still_a_rejection():
    """Order in the rule table is load-bearing. "We'll keep you in mind for future
    interviews" is the single most common way a rejection gets misread as good news."""
    p = classify("Update", "Unfortunately we are not moving forward, but we will keep "
                           "your CV on file for future interviews.")
    assert p.status == "rejected"


def test_the_newest_message_wins_over_the_quoted_thread():
    """A reply quotes the whole conversation, including the invitation that came
    before the rejection. Classifying the thread instead of the message would report
    the outcome backwards."""
    p = classify("Re: Interview", "Unfortunately we will not be proceeding.\n\n"
                                  "On Mon, 3 Aug, Recruiter wrote:\n"
                                  "> We would like to schedule a call with you")
    assert p.status == "rejected"


def test_acknowledgements_are_recognised_but_propose_nothing():
    """A mailbox full of "we received your application" must be distinguishable from
    a detector that has stopped working."""
    p = classify("Application received", "Thanks for applying! We are reviewing it.")
    assert p.status is None and p.kind == "acknowledgement"


def test_job_alerts_are_not_replies():
    """A job-alert digest quotes enough hiring language to trip every rule, so it is
    excluded before the rules run."""
    p = classify("12 new jobs for you", "Recommended jobs matching your profile. "
                                        "Schedule a call with employers. Unsubscribe.")
    assert p.kind == "not_a_reply" and p.status is None


def test_confidence_is_high_only_when_phrases_agree():
    """One idiom is exactly how a polite rejection reads as an invitation, so a single
    match is surfaced as low confidence rather than acted on as fact."""
    single = classify("Hi", "The position has been filled.")
    double = classify("Hi", "Unfortunately, we have decided not to move forward.")
    assert single.confidence == "low"
    assert double.confidence == "high"


def test_empty_input_proposes_nothing():
    assert classify(None, None).status is None
    assert classify("", "").status is None


# --- message parsing ----------------------------------------------------------

def test_encoded_subjects_are_decoded():
    """An RFC2047 subject arrives base64-encoded; left undecoded, every rule silently
    fails to match and the detector reports "nothing found" rather than an error."""
    assert decode_field("=?utf-8?b?VW5mb3J0dW5hdGVseQ==?=") == "Unfortunately"


def test_html_only_mail_is_stripped_to_text():
    msg = EmailMessage()
    msg.set_content("<p>Unfortunately we are <b>not</b> proceeding.</p>", subtype="html")
    text = body_text(msg)
    assert "Unfortunately" in text and "<b>" not in text


def test_multipart_prefers_the_plain_part():
    msg = EmailMessage()
    msg.set_content("plain: we would like to schedule a call")
    msg.add_alternative("<p>html version</p>", subtype="html")
    assert "schedule a call" in body_text(msg)


# --- attribution --------------------------------------------------------------

APPS = [
    {"id": "a1", "company": "Northwind Labs", "title": "Backend Engineer",
     "apply_email": "jobs@northwind.example"},
    {"id": "a2", "company": "Globex", "title": "Data Engineer", "apply_email": ""},
]


def test_a_sender_domain_is_the_strongest_signal():
    assert match_application("no names here", "careers@northwind.example", APPS)["id"] == "a1"


def test_a_company_name_in_the_body_attributes_the_reply():
    assert match_application("Thanks from Globex", "x@mail.com", APPS)["id"] == "a2"


def test_an_unattributable_reply_matches_nothing():
    """Attributing a rejection to the WRONG application closes out a live opportunity
    on the operator's record. Matching nothing is the safe failure."""
    assert match_application("Unfortunately no.", "someone@unknown.com", APPS) is None


def test_a_short_company_name_does_not_match_on_a_substring():
    apps = [{"id": "x", "company": "IBM", "title": "Engineer", "apply_email": ""}]
    assert match_application("limbo timbre", "a@b.com", apps) is None


# --- scanning -----------------------------------------------------------------

class FakeIMAP:
    """Minimal imaplib-compatible stand-in."""

    def __init__(self, messages):
        self._raw = [m.as_bytes() for m in messages]

    def select(self, folder):
        return "OK", [b""]

    def search(self, charset, query):
        return "OK", [b" ".join(str(i).encode() for i in range(len(self._raw)))]

    def fetch(self, mid, spec):
        return "OK", [(b"1", self._raw[int(mid)])]


def _mail(subject, body, sender="careers@northwind.example", msgid="<m1@x>"):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["Message-ID"] = msgid
    msg.set_content(body)
    return msg


@pytest.fixture
def store_with_submitted(tmp_path):
    s = Store(str(tmp_path / "inbox.db"))
    s.init_schema()
    job_id = s.upsert_job(JobPosting(source=Source.remoteok, title="Backend Engineer",
                                     company="Northwind Labs",
                                     apply_email="jobs@northwind.example"))
    app_id = s.create_application(Application(job_id=job_id, apply_method=ApplyMethod.email,
                                              status="submitted"))
    yield s, app_id
    s.close()


def test_a_scan_records_a_proposal_and_changes_nothing(store_with_submitted):
    """The central property: detection proposes, it never decides."""
    store, app_id = store_with_submitted
    reader = InboxReader(FakeIMAP([_mail("Update", "Unfortunately we are not proceeding.")]))

    report = scan(store, reader)
    assert report.proposed == 1
    assert store.get_application(app_id)["status"] == "submitted", (
        "a scan must not move the application"
    )
    pending = store.list_proposals()
    assert len(pending) == 1 and pending[0]["proposed"] == "rejected"


def test_rescanning_the_same_mailbox_does_not_duplicate(store_with_submitted):
    """Polling re-reads the same messages every time; without idempotency the queue
    fills with copies of a decision already made."""
    store, _ = store_with_submitted
    reader = InboxReader(FakeIMAP([_mail("Update", "Unfortunately we are not proceeding.")]))
    scan(store, reader)
    second = scan(store, reader)
    assert second.proposed == 0
    assert len(store.list_proposals()) == 1


def test_a_dismissed_proposal_does_not_come_back(store_with_submitted):
    store, _ = store_with_submitted
    reader = InboxReader(FakeIMAP([_mail("Update", "Unfortunately we are not proceeding.")]))
    scan(store, reader)
    pid = store.list_proposals()[0]["id"]
    store.set_proposal_state(pid, "dismissed")

    scan(store, reader)
    assert store.list_proposals(state="pending") == []


def test_applications_that_were_never_sent_are_not_candidates(tmp_path):
    """An outcome for something never submitted is a misattribution by definition."""
    store = Store(str(tmp_path / "d.db"))
    store.init_schema()
    job_id = store.upsert_job(JobPosting(source=Source.remoteok, title="Backend Engineer",
                                         company="Northwind Labs"))
    store.create_application(Application(job_id=job_id, apply_method=ApplyMethod.email,
                                         status="drafting"))
    reader = InboxReader(FakeIMAP([_mail("Update", "Unfortunately we are not proceeding.")]))
    assert scan(store, reader).proposed == 0
    store.close()


def test_noise_is_counted_rather_than_silently_dropped(store_with_submitted):
    """"Nothing proposed" should be distinguishable from "nothing arrived"."""
    store, _ = store_with_submitted
    reader = InboxReader(FakeIMAP([
        _mail("Application received", "Thanks for applying! We are reviewing it.", msgid="<a@x>"),
        _mail("10 new jobs", "Recommended jobs. Unsubscribe here.", msgid="<b@x>"),
    ]))
    report = scan(store, reader)
    assert report.acknowledgements == 1
    assert report.skipped_not_a_reply == 1
    assert report.proposed == 0


# --- the API ------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    db = str(tmp_path / "api.db")
    monkeypatch.setenv("JOBAGENT_DB_PATH", db)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    monkeypatch.setenv("JOBAGENT_MASTER_KEY", "")
    s = Store(db)
    s.init_schema()
    job_id = s.upsert_job(JobPosting(source=Source.remoteok, title="Backend Engineer",
                                     company="Northwind Labs"))
    app_id = s.create_application(Application(job_id=job_id, apply_method=ApplyMethod.email,
                                              status="submitted"))
    s.add_proposal(application_id=app_id, message_id="<m@x>", proposed="interview",
                   confidence="high", reason="schedule a call", subject="Next steps")
    s.close()

    app = create_app(settings=Settings(_env_file=None), profile=Profile(name="T"),
                     llm=None, cv_master="x", mailer=lambda *a, **k: None)
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {c.post('/auth/login', json={'password': 'pw'}).json()['token']}"
    c._app_id = app_id
    return c


def test_proposals_are_listed_with_their_legality(client):
    rows = client.get("/inbox/proposals").json()["proposals"]
    assert len(rows) == 1
    assert rows[0]["proposed"] == "interview"
    assert rows[0]["current_status"] == "submitted"
    assert rows[0]["is_legal"] is True, "submitted → interview is a legal move"
    assert rows[0]["title"] == "Backend Engineer"


def test_accepting_applies_the_transition_and_audits_the_source(client):
    pid = client.get("/inbox/proposals").json()["proposals"][0]["id"]
    r = client.post(f"/inbox/proposals/{pid}", json={"action": "accept"})
    assert r.status_code == 200 and r.json()["status"] == "interview"
    assert client.get("/applications").json()["applications"][0]["status"] == "interview"
    assert client.get("/inbox/proposals").json()["proposals"] == []


def test_accepting_obeys_the_same_transition_map_as_a_manual_edit(client, tmp_path):
    """The detector gets no privileged path into the lifecycle. A mis-detected outcome
    must not rewrite history just because it arrived by email."""
    store = Store(str(tmp_path / "api.db"))
    app_id = client._app_id
    store.update_application(app_id, status="rejected")     # terminal
    pid = store.add_proposal(application_id=app_id, message_id="<illegal@x>",
                             proposed="interview")
    store.close()

    r = client.post(f"/inbox/proposals/{pid}", json={"action": "accept"})
    assert r.status_code == 422
    assert "Cannot move rejected" in r.json()["detail"]["message"]


def test_dismissing_leaves_the_application_alone(client):
    pid = client.get("/inbox/proposals").json()["proposals"][0]["id"]
    assert client.post(f"/inbox/proposals/{pid}", json={"action": "dismiss"}).status_code == 200
    assert client.get("/applications").json()["applications"][0]["status"] == "submitted"


def test_a_decision_cannot_be_replayed(client):
    pid = client.get("/inbox/proposals").json()["proposals"][0]["id"]
    client.post(f"/inbox/proposals/{pid}", json={"action": "accept"})
    assert client.post(f"/inbox/proposals/{pid}", json={"action": "accept"}).status_code == 409


def test_deciding_requires_auth(client):
    anon = TestClient(client.app)
    assert anon.post("/inbox/proposals/x", json={"action": "accept"}).status_code in (401, 403)
