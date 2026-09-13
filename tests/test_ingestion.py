"""Phase 1 tests: RemoteOK adapter normalization + the ingestion runner.

Uses httpx.MockTransport so no network and no extra deps."""

import httpx

from jobagent.core.schemas import ApplyMethod, Source
from jobagent.ingestion.adapters.remoteok import RemoteOKAdapter
from jobagent.ingestion.runner import run_ingestion
from jobagent.store import Store

# First element mimics RemoteOK's legal-notice header (must be skipped).
_FAKE_FEED = [
    {"legal": "See https://remoteok.com/api for terms"},
    {
        "id": "1001",
        "position": "Senior AI Engineer",
        "company": "Globex",
        "location": "Worldwide",
        "tags": ["ai", "python", "ml"],
        "description": "<p>Build <b>LLM</b> systems.</p>",
        "salary_min": 120000,
        "salary_max": 160000,
        "url": "https://remoteok.com/remote-jobs/1001",
        "apply_url": "https://globex.example/apply",
        "epoch": 1_700_000_000,
    },
    {
        "id": "1002",
        "position": "Backend Engineer",
        "company": "Acme",
        "description": "Go services.",
        "apply_url": "mailto:jobs@acme.example?subject=Application",
        "epoch": 1_700_100_000,
    },
]


def _adapter_with(feed) -> RemoteOKAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("user-agent")  # RemoteOK requires a UA
        return httpx.Response(200, json=feed)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return RemoteOKAdapter(client=client)


def test_remoteok_normalizes_and_skips_legal_header():
    jobs = list(_adapter_with(_FAKE_FEED).fetch())
    assert len(jobs) == 2  # legal header skipped

    ai = jobs[0]
    assert ai.source == Source.remoteok.value
    assert ai.title == "Senior AI Engineer"
    assert ai.is_remote is True
    assert ai.description == "Build LLM systems."  # HTML stripped
    assert ai.salary_text == "$120,000–$160,000"
    assert ai.apply_method == ApplyMethod.external_link.value
    assert ai.posted_at is not None
    assert ai.raw["id"] == "1001"  # full payload preserved


def test_remoteok_detects_mailto_apply():
    jobs = list(_adapter_with(_FAKE_FEED).fetch())
    email_job = jobs[1]
    assert email_job.apply_method == ApplyMethod.email.value
    assert email_job.apply_email == "jobs@acme.example"


def test_runner_counts_new_vs_reseen(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()

    report = run_ingestion([_adapter_with(_FAKE_FEED)], store)
    assert report.total_fetched == 2
    assert report.total_new == 2
    assert report.results[0].error is None

    # Second run: same jobs → fetched again but zero new.
    report2 = run_ingestion([_adapter_with(_FAKE_FEED)], store)
    assert report2.total_fetched == 2
    assert report2.total_new == 0
    store.close()


def test_runner_survives_failing_adapter(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.init_schema()

    def boom(request):
        return httpx.Response(500)

    bad = RemoteOKAdapter(client=httpx.Client(transport=httpx.MockTransport(boom)))
    report = run_ingestion([bad, _adapter_with(_FAKE_FEED)], store)

    assert report.results[0].error is not None      # bad adapter recorded error
    assert report.results[1].new == 2               # good adapter still ran


# --- Himalayas (remote-first board with an explicit location restriction) ----

from jobagent.ingestion.adapters.himalayas import HimalayasAdapter  # noqa: E402

_HIMALAYAS_FEED = {"jobs": [
    {"guid": "abc1", "title": "Senior Backend Engineer", "companyName": "Globex",
     "locationRestrictions": ["Worldwide"], "description": "<p>Python, FastAPI.</p>",
     "applicationLink": "https://himalayas.app/jobs/abc1", "pubDate": 1725500000,
     "categories": ["Software Development"], "seniority": ["Senior"]},
    {"guid": "abc2", "title": "Frontend Engineer", "companyName": "Initech",
     "locationRestrictions": ["United States"], "description": "React.",
     "applicationLink": "https://himalayas.app/jobs/abc2", "pubDate": 1725500000},
    {"guid": "abc3", "title": "Data Engineer", "companyName": "Acme",
     "locationRestrictions": [], "description": "SQL.", "pubDate": "not-a-date"},
]}


def _himalayas_with(feed):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=feed)
    return HimalayasAdapter(client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_himalayas_carries_location_restrictions_into_the_location_field():
    jobs = list(_himalayas_with(_HIMALAYAS_FEED).fetch())
    assert len(jobs) == 3
    j0 = jobs[0]
    assert j0.source == Source.himalayas
    assert j0.title == "Senior Backend Engineer" and j0.company == "Globex"
    assert j0.location == "Worldwide"                 # global → the geo scorer keeps it
    assert j0.is_remote is True
    assert "Python" in j0.description and "<p>" not in j0.description   # HTML stripped
    assert j0.apply_url.endswith("abc1")
    assert "Software Development" in j0.tags and "Senior" in j0.tags
    assert j0.posted_at is not None                   # unix timestamp parsed
    # A US-only restriction is carried verbatim so the geo scorer can region-lock it.
    assert jobs[1].location == "United States"
    # Empty restrictions default to Remote; a bad date degrades to None, not a crash.
    assert jobs[2].location == "Remote" and jobs[2].posted_at is None


def test_himalayas_tolerates_a_non_dict_payload():
    empty = HimalayasAdapter(client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))))
    assert list(empty.fetch()) == []
