"""Tier 3: run-ID spine — one id rides every event of a pipeline pass, so a run can
be reconstructed from the events table instead of guessed at from timestamps."""

import httpx

from jobagent.core.schemas import Event, JobPosting, Source
from jobagent.ingestion.adapters.greenhouse import GreenhouseAdapter
from jobagent.ingestion.runner import run_ingestion
from jobagent.matching import run_matching
from jobagent.preferences import Profile
from jobagent.store import Store

PROFILE = Profile(target_roles=["AI Engineer"], core_skills=["Python"], seniority="mid")


def _store(tmp_path):
    s = Store(str(tmp_path / "o.db"))
    s.init_schema()
    return s


def _gh_client(response):
    return httpx.Client(transport=httpx.MockTransport(lambda req: response))


def test_ingest_events_carry_the_run_id(tmp_path):
    s = _store(tmp_path)
    ok = httpx.Response(200, json={"jobs": [{"id": 1, "title": "AI Engineer",
                                             "absolute_url": "https://x/1", "content": "Python"}]})
    run_ingestion([GreenhouseAdapter(["acme"], client=_gh_client(ok))], s, run_id="run123")
    events = s.events_for_run("run123")
    assert [e["kind"] for e in events] == ["ingest"]
    assert events[0]["source"] == "greenhouse" and events[0]["fetched"] == 1
    s.close()


def test_error_events_carry_the_run_id_too(tmp_path):
    """Failures are exactly what reconstruction is for — they must not lose the id."""
    class BoomAdapter(GreenhouseAdapter):
        def fetch(self):
            raise RuntimeError("boom")

    s = _store(tmp_path)
    run_ingestion([BoomAdapter(["acme"])], s, run_id="runERR")
    events = s.events_for_run("runERR")
    assert [e["kind"] for e in events] == ["error"]
    assert "boom" in events[0]["error"]
    s.close()


def test_matching_logs_an_event_at_all(tmp_path):
    """Matching previously logged nothing — a crashed or zero-job matching pass was
    indistinguishable from one that never ran."""
    s = _store(tmp_path)
    s.upsert_job(JobPosting(source=Source.remoteok, title="AI Engineer", company="A",
                            description="Python"))
    run_matching(s, PROFILE, run_id="runM")
    events = s.events_for_run("runM")
    assert [e["kind"] for e in events] == ["match"]
    assert events[0]["scored"] == 1 and events[0]["used_llm"] is False
    s.close()


def test_one_run_id_ties_ingest_and_match_together(tmp_path):
    s = _store(tmp_path)
    ok = httpx.Response(200, json={"jobs": [{"id": 1, "title": "AI Engineer",
                                             "absolute_url": "https://x/1", "content": "Python"}]})
    run_ingestion([GreenhouseAdapter(["acme"], client=_gh_client(ok))], s, run_id="runBOTH")
    run_matching(s, PROFILE, run_id="runBOTH")
    kinds = [e["kind"] for e in s.events_for_run("runBOTH")]
    assert kinds == ["ingest", "match"]           # ordered, complete, one id
    s.close()


def test_run_ledger_lists_summaries_newest_first(tmp_path):
    s = _store(tmp_path)
    for i in (1, 2):
        s.log_event(Event(kind="run", payload={
            "run_id": f"r{i}", "duration_s": i * 1.5,
            "ingest": {"fetched": 10 * i, "new": i, "errors": []},
            "match": {"scored": 5, "llm_reranked": 0},
            "digest": "skipped (--no-send)",
        }))
    runs = s.list_runs()
    assert [r["run_id"] for r in runs] == ["r2", "r1"]
    assert runs[0]["ingest"]["fetched"] == 20
    assert "finished_at" in runs[0]
    s.close()


def test_events_for_unknown_run_is_empty(tmp_path):
    s = _store(tmp_path)
    assert s.events_for_run("nope") == []
    s.close()


def test_events_without_run_id_do_not_pollute_a_run(tmp_path):
    """Pre-spine events (and bot-path events) have no run_id — they must not attach
    themselves to anyone's run."""
    s = _store(tmp_path)
    s.log_event(Event(kind="ingest", payload={"source": "remoteok", "fetched": 1, "new": 1}))
    s.log_event(Event(kind="ingest", payload={"source": "lever", "fetched": 2, "new": 0,
                                              "run_id": "mine"}))
    events = s.events_for_run("mine")
    assert len(events) == 1 and events[0]["source"] == "lever"
    s.close()
