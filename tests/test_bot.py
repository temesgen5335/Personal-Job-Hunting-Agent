"""Bot tests: pure service helpers + notifier. No telegram lib, no network."""

import httpx
import pytest

from jobagent.bot.notify import chunk_text, send_message
from jobagent.bot.service import is_owner, jobs_text, status_text
from jobagent.core.schemas import JobPosting, Match, Source
from jobagent.store import Store


def test_is_owner_fails_closed():
    assert is_owner(123, 123) is True
    assert is_owner(123, 999) is False
    assert is_owner(123, None) is False   # no owner configured → deny
    assert is_owner(None, 123) is False


def test_status_text_renders_counts():
    text = status_text({
        "total_jobs": 7000, "by_source": {"greenhouse": 5000, "telegram": 222},
        "matches": 7000, "strong_matches": 120, "last_ingest": "2026-06-18T00:00:00",
    })
    assert "Total jobs: 7000" in text
    assert "greenhouse: 5000" in text
    assert "strong ≥70%: 120" in text


def test_jobs_text_from_store(tmp_path):
    store = Store(str(tmp_path / "b.db"))
    store.init_schema()
    jid = store.upsert_job(JobPosting(source=Source.remoteok, title="AI Engineer",
                                      company="Acme", is_remote=True))
    store.upsert_match(Match(job_id=jid, score=0.9, rationale="great fit"))
    text = jobs_text(store, 5)
    assert "AI Engineer" in text
    assert "90%" in text
    store.close()


def test_chunk_text_splits_on_newline_under_limit():
    body = "\n".join(f"line {i}" for i in range(1000))
    chunks = list(chunk_text(body, size=200))
    assert all(len(c) <= 200 for c in chunks)
    assert "".join(c if c.startswith("line 0") else "\n" + c for c in chunks).count("line") == 1000


def test_send_message_chunks_and_posts():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    long_text = "x" * 9000  # forces 3 chunks at 4000
    sent = send_message("tok", 123, long_text, client=client)
    assert sent == 3
    assert len(calls) == 3
    assert "/bottok/sendMessage" in str(calls[0].url)


def test_send_message_requires_creds():
    with pytest.raises(ValueError):
        send_message("", 123, "hi")
    with pytest.raises(ValueError):
        send_message("tok", None, "hi")
