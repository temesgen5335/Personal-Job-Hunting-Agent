"""Ingest gate: filter postings before they are stored.

The gate is irreversible (unlike scoring), so these tests pin the two properties that
keep it safe: it only rejects on the stated axes, and every drop is counted and
surfaced rather than silent.
"""

from datetime import datetime, timedelta, timezone

import httpx

from jobagent.config import Settings
from jobagent.core.schemas import JobPosting, Source
from jobagent.digest import health_banner
from jobagent.ingestion.adapters.greenhouse import GreenhouseAdapter
from jobagent.ingestion.gate import ALL_SOURCES, IngestGate, resolve_sources
from jobagent.ingestion.runner import run_ingestion
from jobagent.preferences import Sources
from jobagent.store import Store


def _job(**kw) -> JobPosting:
    base = dict(source=Source.remoteok, title="AI Engineer", company="Acme",
                location="Remote", is_remote=True, description="Python work.")
    base.update(kw)
    return JobPosting(**base)


def _days_ago(n: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=n)


# --- age ---------------------------------------------------------------------------

def test_inactive_gate_keeps_everything():
    g = IngestGate()
    assert g.active is False
    assert g.reject(_job(posted_at=_days_ago(900))) is None


def test_age_limit_drops_old_and_keeps_fresh():
    g = IngestGate(max_age_days=14)
    assert g.reject(_job(posted_at=_days_ago(30))) == "too_old"
    assert g.reject(_job(posted_at=_days_ago(3))) is None


def test_unknown_posted_at_is_kept():
    """Telegram posts and some boards omit the date. Dropping unknown-age postings
    would quietly delete an entire source's output."""
    assert IngestGate(max_age_days=7).reject(_job(posted_at=None)) is None


def test_naive_posted_at_is_treated_as_utc_not_crashed():
    g = IngestGate(max_age_days=14)
    naive_old = datetime.now() - timedelta(days=40)      # no tzinfo
    assert g.reject(_job(posted_at=naive_old)) == "too_old"


# --- locations (multi-value) --------------------------------------------------------

def test_multiple_locations_are_or_matched():
    g = IngestGate(locations=["EMEA", "Africa"])
    assert g.reject(_job(location="Remote - EMEA", is_remote=False)) is None
    assert g.reject(_job(location="Nairobi, Africa", is_remote=False)) is None
    assert g.reject(_job(location="Tokyo, Japan", is_remote=False)) == "location"


def test_remote_term_trusts_the_structured_flag():
    """'remote' as a location term should accept is_remote=True even when the
    location string says nothing useful."""
    g = IngestGate(locations=["remote"])
    assert g.reject(_job(location="", is_remote=True)) is None
    assert g.reject(_job(location="Munich office", is_remote=False)) == "location"


def test_location_terms_are_word_boundary_matched():
    """A term of "US" must not match "Belarus" or "Austria" — the substring bug this
    codebase has already hit twice ("Go" in "going", "cto" in "Connectors")."""
    g = IngestGate(locations=["US"])
    assert g.reject(_job(location="Minsk, Belarus", is_remote=False)) == "location"
    assert g.reject(_job(location="Vienna, Austria", is_remote=False)) == "location"
    assert g.reject(_job(location="Austin, US", is_remote=False)) is None


def test_location_also_looks_at_the_title():
    g = IngestGate(locations=["EMEA"])
    assert g.reject(_job(title="Backend Engineer (EMEA)", location="", is_remote=False)) is None


# --- drop keywords -----------------------------------------------------------------

def test_drop_keywords_scan_title_description_and_tags():
    g = IngestGate(drop_keywords=["unpaid", "clearance required"])
    assert g.reject(_job(description="This is an unpaid internship.")) == "keyword"
    assert g.reject(_job(title="Engineer (unpaid)")) == "keyword"
    assert g.reject(_job(tags=["unpaid"])) == "keyword"
    assert g.reject(_job(description="Well paid role.")) is None


def test_drop_keywords_are_word_boundary_matched():
    g = IngestGate(drop_keywords=["AI"])
    assert g.reject(_job(description="Plain SAID nothing", title="Role")) is None
    assert g.reject(_job(description="We build AI systems")) == "keyword"


# --- settings wiring ---------------------------------------------------------------

def test_from_settings_reads_comma_lists_and_zero_means_no_limit():
    s = Settings(_env_file=None, INGEST_MAX_AGE_DAYS="0",
                 INGEST_LOCATIONS=" remote , EMEA ", INGEST_DROP_KEYWORDS="unpaid")
    g = IngestGate.from_settings(s)
    assert g.max_age_days is None                      # 0 → no age limit
    assert g.locations == ["remote", "EMEA"]           # trimmed
    assert g.drop_keywords == ["unpaid"]
    assert "location in [remote, EMEA]" in g.describe()


def test_from_settings_all_blank_is_inactive():
    assert IngestGate.from_settings(Settings(_env_file=None)).active is False


# --- source selection --------------------------------------------------------------

def test_ingest_sources_overrides_the_toml_toggles():
    toml = Sources(remoteok=True, remotive=True, greenhouse=True, lever=True,
                   ashby=True, telegram=True)
    s = Settings(_env_file=None, INGEST_SOURCES="remoteok, telegram")
    assert resolve_sources(s, toml) == {"remoteok", "telegram"}


def test_blank_ingest_sources_falls_back_to_toml():
    toml = Sources(remoteok=True, remotive=False, greenhouse=False, lever=False,
                   ashby=False, telegram=False)
    assert resolve_sources(Settings(_env_file=None), toml) == {"remoteok"}


def test_source_selection_is_case_insensitive():
    s = Settings(_env_file=None, INGEST_SOURCES="RemoteOK,TELEGRAM")
    assert resolve_sources(s, Sources()) == {"remoteok", "telegram"}


def test_every_toggleable_source_is_offered_to_the_ui():
    """A new adapter without an ALL_SOURCES entry would be unselectable in the UI."""
    assert set(Sources.model_fields) == set(ALL_SOURCES)


# --- runner integration + drop accounting -----------------------------------------

def _gh_client(payload):
    return httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json=payload)))


def _board(*titles, days_old=1):
    posted = _days_ago(days_old).isoformat()
    return {"jobs": [
        {"id": i, "title": t, "absolute_url": f"https://x/{i}",
         "content": "Python role", "updated_at": posted}
        for i, t in enumerate(titles, 1)
    ]}


def test_gate_drops_are_counted_per_reason_and_logged(tmp_path):
    s = Store(str(tmp_path / "g.db"))
    s.init_schema()
    payload = _board("AI Engineer", "Unpaid Intern", "Backend Engineer")
    gate = IngestGate(drop_keywords=["unpaid"])
    report = run_ingestion([GreenhouseAdapter(["acme"], client=_gh_client(payload))],
                          s, run_id="r1", gate=gate)

    assert report.total_fetched == 3          # the adapter yielded three
    assert report.total_dropped == 1          # one rejected
    assert report.results[0].kept == 2        # two stored
    assert report.drops_by_reason == {"keyword": 1}
    assert s.count_jobs() == 2                # the dropped one never entered the store

    # And the drop is visible in the ledger, not silent.
    ev = [e for e in s.events_for_run("r1") if e["kind"] == "ingest"][0]
    assert ev["dropped"] == 1 and ev["kept"] == 2 and ev["drops"] == {"keyword": 1}
    s.close()


def test_no_gate_stores_everything(tmp_path):
    s = Store(str(tmp_path / "n.db"))
    s.init_schema()
    report = run_ingestion([GreenhouseAdapter(["acme"], client=_gh_client(_board("A", "B")))],
                           s, run_id="r2")
    assert report.total_dropped == 0 and s.count_jobs() == 2
    s.close()


def test_digest_warns_when_the_gate_ate_everything(tmp_path):
    """Sources answered but nothing was stored — almost always a mis-set filter, and
    indistinguishable from a dead pipeline without the warning."""
    s = Store(str(tmp_path / "a.db"))
    s.init_schema()
    gate = IngestGate(drop_keywords=["python"])          # matches every fixture
    report = run_ingestion([GreenhouseAdapter(["acme"], client=_gh_client(_board("A", "B")))],
                           s, run_id="r3", gate=gate)
    assert report.total_dropped == report.total_fetched == 2
    banner = health_banner(report, {"sources": []})
    assert "filtered out all 2" in banner
    s.close()


def test_partial_filtering_does_not_trigger_the_all_filtered_warning(tmp_path):
    s = Store(str(tmp_path / "p.db"))
    s.init_schema()
    report = run_ingestion(
        [GreenhouseAdapter(["acme"], client=_gh_client(_board("Unpaid Intern", "AI Engineer")))],
        s, run_id="r4", gate=IngestGate(drop_keywords=["unpaid"]))
    assert report.total_dropped == 1
    assert health_banner(report, {"sources": []}) == ""     # no noise on a normal run
    s.close()
