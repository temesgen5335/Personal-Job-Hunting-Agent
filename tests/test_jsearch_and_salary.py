"""The aggregator adapter, salary parsing, and cross-board clustering.

No network: the adapter is driven through an injected httpx client with a canned
payload shaped like a real JSearch response (R17).
"""

import httpx
import pytest

from jobagent.core.schemas import JobPosting, Source, normalize_company, normalize_title
from jobagent.ingestion.adapters.jsearch import JSearchAdapter
from jobagent.salary import Salary, infer_period, parse_salary
from jobagent.store import Store

# Field names taken from a real JSearch response, not from memory. Guessed keys are the
# defect class this project has hit four times (R32) — and here a wrong key would render
# every posting as "(untitled)" at no other cost, which is how it would go unnoticed.
_ITEM = {
    "job_id": "abc123",
    "job_title": "Senior Backend Engineer",
    "employer_name": "Northwind Labs",
    "job_city": "Berlin",
    "job_country": "DE",
    "job_is_remote": True,
    "job_description": "<p>Build <b>things</b>.</p>",
    "job_apply_link": "https://example.com/apply/abc123",
    "job_publisher": "LinkedIn",
    "job_min_salary": 90000,
    "job_max_salary": 120000,
    "job_salary_currency": "EUR",
    "job_salary_period": "YEAR",
    "job_posted_at_timestamp": 1_755_000_000,
}


def _client(payload, capture=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


# --- the adapter --------------------------------------------------------------

def test_it_normalizes_a_real_response_shape():
    jobs = list(JSearchAdapter("k", ["backend"], client=_client({"data": [_ITEM]})).fetch())
    assert len(jobs) == 1
    job = jobs[0]

    assert job.source == Source.aggregator
    assert job.title == "Senior Backend Engineer"
    assert job.company == "Northwind Labs"
    assert job.location == "Berlin, DE"
    assert job.is_remote is True
    assert job.description == "Build things."          # HTML stripped
    assert job.apply_url == "https://example.com/apply/abc123"
    assert job.posted_at is not None
    # Which underlying board it came from is how you spot a LinkedIn duplicate of a
    # Greenhouse original, so it must survive normalization.
    assert "LinkedIn" in job.tags
    assert job.raw == _ITEM, "the untouched payload is never discarded"


def test_salary_fields_are_composed_into_text_the_shared_parser_can_read():
    """JSearch splits salary across four fields. Composing a string here keeps ONE
    parsing code path for every source rather than a special case per adapter."""
    job = next(iter(JSearchAdapter("k", ["x"], client=_client({"data": [_ITEM]})).fetch()))
    pay = parse_salary(job.salary_text)
    assert (pay.min, pay.max, pay.currency, pay.period) == (90000, 120000, "EUR", "year")


def test_it_is_disabled_without_a_key_or_without_queries():
    """An empty query would ask the API for every job on the internet and return an
    arbitrary slice of it — thousands of rows unrelated to the profile, which is worse
    than fetching nothing."""
    assert not JSearchAdapter("", ["backend"]).enabled
    assert not JSearchAdapter("key", []).enabled
    assert not JSearchAdapter("key", ["  "]).enabled
    assert JSearchAdapter("key", ["backend"]).enabled


def test_a_disabled_adapter_makes_no_request():
    calls: list[httpx.Request] = []
    assert list(JSearchAdapter("", ["x"], client=_client({"data": [_ITEM]}, calls)).fetch()) == []
    assert calls == [], "a disabled adapter must not spend a quota call"


def test_it_sends_the_rapidapi_credentials_and_the_query():
    calls: list[httpx.Request] = []
    list(JSearchAdapter("secret-key", ["backend engineer"], location="Berlin",
                        client=_client({"data": []}, calls)).fetch())
    assert calls, "no request was made"
    request = calls[0]
    assert request.headers["x-rapidapi-key"] == "secret-key"
    assert "backend+engineer" in str(request.url) or "backend%20engineer" in str(request.url)
    assert "Berlin" in str(request.url)
    assert "remote_jobs_only=true" in str(request.url)


def test_queries_are_deduplicated_and_capped():
    """Free RapidAPI tiers are a few hundred calls a month and one call is one query
    per page, so an unbounded profile would burn the month in a single run."""
    adapter = JSearchAdapter("k", ["a", "a", "b", "c", "d", "e", "f", "g"])
    assert adapter.queries == ["a", "b", "c", "d", "e"]


def test_a_row_missing_everything_still_yields_a_usable_posting():
    """Aggregator data is messy; one bad row must not abort the source."""
    jobs = list(JSearchAdapter("k", ["x"], client=_client({"data": [{}]})).fetch())
    assert len(jobs) == 1 and jobs[0].title == "(untitled)"
    assert jobs[0].salary_text is None


# --- salary parsing -----------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("$120,000 - $160,000 per year", (120000, 160000, "USD", "year")),
    ("$120k-160k", (120000, 160000, "USD", None)),
    ("€90.000 - €120.000 per year", (90000, 120000, "EUR", "year")),   # de-DE separators
    ("$55/hour", (55, 55, "USD", "hour")),
    ("Up to £85,000 per annum", (85000, 85000, "GBP", "year")),
    ("USD 8,000 per month", (8000, 8000, "USD", "month")),
    ("55 - 70 USD per hour", (55, 70, "USD", "hour")),
])
def test_real_world_salary_formats(text, expected):
    pay = parse_salary(text)
    assert (pay.min, pay.max, pay.currency, pay.period) == expected


@pytest.mark.parametrize("text", [
    "", None, "Competitive salary", "0.1% - 0.5% equity", "Team of 12 engineers",
    "5+ years of experience", "Founded 2019",
])
def test_it_refuses_to_guess(text):
    """A wrong number is far worse than no number: it would filter out a well-paid job
    invisibly. Anything ambiguous must return nothing."""
    assert parse_salary(text) == Salary()


def test_the_upper_bound_suffix_applies_to_both_ends():
    """"120-160k" means 120,000-160,000, not 120-160,000 — a thousand-fold error, and
    in the direction that hides jobs from a minimum-salary filter."""
    pay = parse_salary("$120-160k per year")
    assert (pay.min, pay.max) == (120000, 160000)


def test_annualisation_compares_like_with_like():
    hourly = parse_salary("$55/hour")
    yearly = parse_salary("$100,000 per year")
    assert hourly.annual_max == 55 * 2080
    assert yearly.annual_max == 100000


def test_an_unknown_period_annualises_to_none_rather_than_guessing():
    """Assuming "year" would make "$50/hour" rank below every salaried job."""
    pay = parse_salary("$120k-160k")
    assert pay.period is None and pay.annual_max is None
    # The guess exists, but only for display, and only behind its own function.
    assert infer_period(pay) == "year"


def test_salary_is_parsed_into_columns_at_write_time(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.init_schema()
    job_id = store.upsert_job(JobPosting(
        source=Source.remoteok, title="Engineer", company="Acme",
        salary_text="$120,000 - $160,000 per year"))
    row = store.get_job(job_id)
    assert (row["salary_min"], row["salary_max"]) == (120000, 160000)
    assert (row["salary_currency"], row["salary_period"]) == ("USD", "year")
    # And the original text survives — the parse is a convenience, not a replacement.
    assert row["salary_text"] == "$120,000 - $160,000 per year"
    store.close()


# --- cross-board clustering ---------------------------------------------------

def _key(title, company):
    return JobPosting(source=Source.remoteok, title=title, company=company).cluster_key()


def test_the_same_role_on_three_boards_shares_one_cluster():
    """The reason this exists: turning on an aggregator triples the queue with the
    same roles under slightly different titles."""
    keys = {
        _key("Senior Backend Engineer", "Acme Inc."),
        _key("Senior Backend Engineer (Remote)", "ACME"),
        _key("Backend Engineer — Berlin", "Acme Technologies"),
    }
    assert len(keys) == 1


def test_genuinely_different_roles_stay_apart():
    """The failure mode that would matter more: over-clustering hides real jobs."""
    assert _key("Backend Engineer", "Acme") != _key("Frontend Engineer", "Acme")
    assert _key("Backend Engineer", "Acme") != _key("Backend Engineer", "Globex")


def test_clustering_never_changes_the_primary_key():
    """dedup_hash is the PK; every application and triage row is keyed on it. Changing
    its meaning would re-id the store and orphan the operator's own history."""
    a = JobPosting(source=Source.remoteok, title="Senior Backend Engineer", company="Acme")
    b = JobPosting(source=Source.lever, title="Senior Backend Engineer (Remote)", company="ACME")
    assert a.cluster_key() == b.cluster_key()
    assert a.dedup_hash() != b.dedup_hash(), (
        "distinct postings must remain distinct ROWS — each keeps its own apply URL"
    )


def test_a_short_title_is_not_eaten_by_the_trailing_place_rule():
    """"Data Engineer" must not become "Data": the trailing-location strip has to leave
    a role behind, or half the queue collapses into one meaningless cluster."""
    assert normalize_title("Data Engineer") == "data engineer"
    assert normalize_title("Engineer") == "engineer"


def test_normalisation_keeps_the_distinctive_part_of_a_company():
    assert normalize_company("Acme Inc.") == normalize_company("ACME") == "acme"
    assert normalize_company("Northwind Labs") == "northwind"
    assert normalize_company("Acme") != normalize_company("Globex")


def test_the_cluster_key_is_stored(tmp_path):
    store = Store(str(tmp_path / "c.db"))
    store.init_schema()
    a = store.upsert_job(JobPosting(source=Source.remoteok,
                                    title="Senior Backend Engineer", company="Acme Inc."))
    b = store.upsert_job(JobPosting(source=Source.lever,
                                    title="Backend Engineer (Remote)", company="ACME"))
    assert a != b, "two rows"
    rows = {r["id"]: r["cluster_key"] for r in store.get_jobs()}
    assert rows[a] == rows[b], "one cluster"
    store.close()


# --- score provenance ---------------------------------------------------------

def test_a_heuristic_rerun_does_not_erase_an_llm_score(tmp_path):
    """The July 2026 audit gap: heuristic scores overwrote LLM scores every run and no
    provenance was kept, so an expensive rerank silently vanished on the next pass and
    nothing recorded that a better number had ever existed."""
    from jobagent.core.schemas import Match

    store = Store(str(tmp_path / "p.db"))
    store.init_schema()
    job_id = store.upsert_job(JobPosting(source=Source.remoteok, title="Engineer",
                                         company="Acme"))

    store.upsert_match(Match(job_id=job_id, score=0.4, rationale="heuristic"))
    store.upsert_match(Match(job_id=job_id, score=0.9, rationale="[LLM] strong",
                             score_source="llm", llm_score=0.9))
    assert store.get_match(job_id)["score_source"] == "llm"

    # The next pipeline pass re-scores heuristically, exactly as run_matching does.
    store.upsert_match(Match(job_id=job_id, score=0.4, rationale="heuristic"))
    row = store.get_match(job_id)
    assert row["score"] == 0.4, "the live score is the newest one, as before"
    assert row["score_source"] == "heuristic", "and it says so"
    assert row["llm_score"] == 0.9, "but the rerank that cost quota survives"
    store.close()


def test_score_source_defaults_to_heuristic_on_an_old_row(tmp_path):
    """Migrated rows predate the column; they must read as heuristic, not NULL — a
    NULL here would render as `None` in the assistant's tool output (R32)."""
    from jobagent.core.schemas import Match

    store = Store(str(tmp_path / "d.db"))
    store.init_schema()
    job_id = store.upsert_job(JobPosting(source=Source.remoteok, title="E", company="A"))
    store.upsert_match(Match(job_id=job_id, score=0.5))
    assert store.get_match(job_id)["score_source"] == "heuristic"
    assert store.get_match(job_id)["llm_score"] is None
    store.close()
