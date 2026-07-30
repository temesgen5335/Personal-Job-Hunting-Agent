"""End-to-end pipeline: ingest → match → (optionally) push digest to Telegram.

This is the single command the systemd timer runs on a schedule. Each stage is
independent and logged, so a failure in one is visible without killing the others.

Usage:
    python scripts/pipeline.py            # ingest, match, send digest
    python scripts/pipeline.py --no-send  # ingest + match only (no Telegram)
    python scripts/pipeline.py --top 15
"""

import argparse
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobagent.bot.notify import send_message  # noqa: E402
from jobagent.bot.service import jobs_text  # noqa: E402
from jobagent.config import get_settings  # noqa: E402
from jobagent.core.schemas import Event  # noqa: E402
from jobagent.digest import format_followups, health_banner  # noqa: E402
from jobagent.ingestion.registry import build_adapters  # noqa: E402
from jobagent.ingestion.runner import run_ingestion  # noqa: E402
from jobagent.llm_client import build_llm  # noqa: E402
from jobagent.matching import run_matching  # noqa: E402
from jobagent.preferences import load_preferences  # noqa: E402
from jobagent.store import Store  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=10, help="jobs in the digest")
    parser.add_argument("--no-send", action="store_true", help="skip the Telegram push")
    args = parser.parse_args()

    settings = get_settings()
    profile = load_preferences().profile
    store = Store(settings.db_path)
    store.init_schema()

    # One id for the whole pass — every event below carries it, so a slow or failing
    # run is reconstructable from the events table (store.events_for_run).
    run_id = uuid.uuid4().hex[:12]
    started = time.monotonic()

    # One pass at a time (M5): a timer firing during a manual run would double-fetch
    # sources and interleave the ledger. The TTL frees a crashed holder's lock.
    if not store.try_acquire_lock("pipeline", run_id):
        print("[run] another pipeline pass holds the lock — exiting (stale locks expire after 2h)")
        store.close()
        return
    print(f"[run] {run_id}")
    try:

        # 1) Ingest
        report = run_ingestion(build_adapters(settings), store, run_id=run_id)
        print(f"[ingest] {report.total_new} new / {report.total_fetched} fetched")
        for r in report.results:
            if r.error:
                print(f"[ingest]   {r.source}: ERROR {r.error}")

        # 2) Match
        llm = build_llm(settings)
        mreport = run_matching(store, profile, llm=llm, run_id=run_id)
        mode = f"heuristic+LLM ({' → '.join(llm.chain)})" if mreport.used_llm else "heuristic"
        print(f"[match] scored {mreport.scored} ({mode}); LLM-reranked {mreport.llm_reranked}")

        # 3) Digest — carries a health banner so a degraded run announces itself.
        health = store.pipeline_health()
        banner = health_banner(report, health)
        # Quiet applications ride along with the digest rather than needing their own run.
        followups = format_followups(store.applications_needing_followup())
        if banner:
            print("[health] " + banner.strip().replace("\n", "\n[health] "))
        if args.no_send:
            digest_status = "skipped (--no-send)"
            print("[digest] skipped (--no-send)")
        elif not (settings.telegram_bot_token and settings.telegram_destination):
            digest_status = "skipped (no bot creds)"
            print("[digest] skipped (no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        else:
            try:
                sent = send_message(
                    settings.telegram_bot_token, settings.telegram_destination,
                    banner + jobs_text(store, args.top) + followups,
                )
                digest_status = f"sent ({sent} message(s))"
                print(f"[digest] sent in {sent} message(s)")
            except Exception as exc:  # noqa: BLE001 — report, don't fail the whole run
                digest_status = f"failed: {exc}"
                print(f"[digest] send failed: {exc}")

        # 4) Run summary — the ledger row `store.list_runs()` and GET /runs read.
        store.log_event(Event(kind="run", payload={
            "run_id": run_id,
            "duration_s": round(time.monotonic() - started, 1),
            "ingest": {"fetched": report.total_fetched, "new": report.total_new,
                       "errors": [r.source for r in report.results if r.error]},
            "match": {"scored": mreport.scored, "llm_reranked": mreport.llm_reranked},
            "digest": digest_status,
        }))
        print(f"[run] {run_id} done in {round(time.monotonic() - started, 1)}s")
    finally:
        # An exception in any stage must still free the lock — the TTL is the
        # crash backstop, not the normal path.
        store.release_lock("pipeline", run_id)
        store.close()


if __name__ == "__main__":
    main()
