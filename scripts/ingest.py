"""Run one ingestion pass with all configured adapters.

Usage: python scripts/ingest.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobagent.config import get_settings  # noqa: E402
from jobagent.ingestion.registry import build_adapters  # noqa: E402
from jobagent.ingestion.runner import run_ingestion  # noqa: E402
from jobagent.store import Store  # noqa: E402


def main() -> None:
    settings = get_settings()
    store = Store(settings.db_path)
    store.init_schema()
    report = run_ingestion(build_adapters(settings), store)
    for r in report.results:
        status = r.error or f"fetched={r.fetched} new={r.new}"
        print(f"  {r.source}: {status}")
    print(f"Total: {report.total_new} new / {report.total_fetched} fetched")
    store.close()


if __name__ == "__main__":
    main()
