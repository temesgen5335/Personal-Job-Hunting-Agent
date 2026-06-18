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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobagent.bot.notify import send_message  # noqa: E402
from jobagent.bot.service import jobs_text  # noqa: E402
from jobagent.config import get_settings  # noqa: E402
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

    # 1) Ingest
    report = run_ingestion(build_adapters(settings), store)
    print(f"[ingest] {report.total_new} new / {report.total_fetched} fetched")
    for r in report.results:
        if r.error:
            print(f"[ingest]   {r.source}: ERROR {r.error}")

    # 2) Match
    llm = build_llm(settings)
    mreport = run_matching(store, profile, llm=llm)
    mode = f"heuristic+LLM ({' → '.join(llm.chain)})" if mreport.used_llm else "heuristic"
    print(f"[match] scored {mreport.scored} ({mode}); LLM-reranked {mreport.llm_reranked}")

    # 3) Digest
    if args.no_send:
        print("[digest] skipped (--no-send)")
    elif not (settings.telegram_bot_token and settings.telegram_destination):
        print("[digest] skipped (no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
    else:
        sent = send_message(
            settings.telegram_bot_token, settings.telegram_destination, jobs_text(store, args.top)
        )
        print(f"[digest] sent in {sent} message(s)")

    store.close()


if __name__ == "__main__":
    main()
