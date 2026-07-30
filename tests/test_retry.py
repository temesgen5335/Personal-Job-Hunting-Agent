"""M3: bounded retry + backoff for source HTTP (R8).

No network and no real sleeping — `sleep` and `rng` are injected, so the recorded
delays are asserted exactly instead of being timed.
"""

import httpx
import pytest

from jobagent.ingestion.util import get_with_retry

URL = "https://board.test/jobs"


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _counting(responses):
    """Handler that walks a list of responses/exceptions, one per call."""
    calls = []

    def handler(request):
        calls.append(1)
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    return handler, calls


def test_retries_5xx_then_succeeds():
    handler, calls = _counting([httpx.Response(503), httpx.Response(503), httpx.Response(200, json={"ok": True})])
    slept = []
    r = get_with_retry(_client(handler), URL, sleep=slept.append, rng=lambda: 1.0)
    assert r.json() == {"ok": True}
    assert len(calls) == 3
    assert slept == [0.5, 1.0]          # exponential, jitter pinned to its ceiling


def test_does_not_retry_permanent_404():
    """A wrong company slug is permanent — spending three attempts on it is waste."""
    handler, calls = _counting([httpx.Response(404)])
    slept = []
    with pytest.raises(httpx.HTTPStatusError):
        get_with_retry(_client(handler), URL, sleep=slept.append)
    assert len(calls) == 1 and slept == []


def test_exhausts_attempts_then_raises():
    handler, calls = _counting([httpx.Response(503)])
    with pytest.raises(httpx.HTTPStatusError):
        get_with_retry(_client(handler), URL, attempts=3, sleep=lambda s: None, rng=lambda: 1.0)
    assert len(calls) == 3               # bounded — not an infinite loop


def test_retries_transport_error_then_succeeds():
    handler, calls = _counting([httpx.ConnectTimeout("boom"), httpx.Response(200)])
    r = get_with_retry(_client(handler), URL, sleep=lambda s: None, rng=lambda: 1.0)
    assert r.status_code == 200 and len(calls) == 2


def test_exhausted_transport_error_reraises_the_original():
    handler, _ = _counting([httpx.ConnectTimeout("dead host")])
    with pytest.raises(httpx.ConnectTimeout):
        get_with_retry(_client(handler), URL, attempts=2, sleep=lambda s: None, rng=lambda: 1.0)


def test_honors_numeric_retry_after():
    handler, _ = _counting([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)])
    slept = []
    get_with_retry(_client(handler), URL, sleep=slept.append, rng=lambda: 1.0)
    assert slept == [7.0]                # the server's number wins over our backoff


def test_retry_after_http_date_falls_back_to_backoff():
    """The HTTP-date form is unparseable here; it must degrade, not crash."""
    handler, _ = _counting([
        httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        httpx.Response(200),
    ])
    slept = []
    get_with_retry(_client(handler), URL, sleep=slept.append, rng=lambda: 1.0)
    assert slept == [0.5]


def test_hostile_retry_after_is_capped():
    """A server asking for a 12-day wait must not wedge the run."""
    handler, _ = _counting([httpx.Response(429, headers={"Retry-After": "999999"}), httpx.Response(200)])
    slept = []
    get_with_retry(_client(handler), URL, sleep=slept.append, rng=lambda: 1.0)
    assert slept == [60.0]


def test_backoff_is_clamped_to_max_delay():
    handler, _ = _counting([httpx.Response(503)])
    slept = []
    with pytest.raises(httpx.HTTPStatusError):
        get_with_retry(_client(handler), URL, attempts=6, base_delay=1.0, max_delay=4.0,
                       sleep=slept.append, rng=lambda: 1.0)
    assert slept == [1.0, 2.0, 4.0, 4.0, 4.0]   # doubles, then clamps


def test_jitter_spreads_retries():
    """Full jitter: the delay scales with rng, so simultaneous failures don't
    come back in lockstep."""
    handler, _ = _counting([httpx.Response(503), httpx.Response(200)])
    slept = []
    get_with_retry(_client(handler), URL, sleep=slept.append, rng=lambda: 0.25)
    assert slept == [0.125]                      # 0.5 ceiling * 0.25


def test_rate_limited_board_yields_nothing_but_does_not_kill_the_run(monkeypatch, tmp_path):
    """End-to-end: an exhausted source degrades to 'no jobs from here', and the
    other adapters in the same run are unaffected."""
    monkeypatch.setattr("jobagent.ingestion.util.time.sleep", lambda s: None)
    from jobagent.ingestion.adapters.greenhouse import GreenhouseAdapter
    from jobagent.ingestion.runner import run_ingestion
    from jobagent.store import Store

    handler, calls = _counting([httpx.Response(429)])
    store = Store(str(tmp_path / "r.db"))
    store.init_schema()
    try:
        report = run_ingestion([GreenhouseAdapter(["acme"], client=_client(handler))], store)
    finally:
        store.close()
    assert report.total_new == 0
    assert len(calls) == 3          # it did retry before giving up
