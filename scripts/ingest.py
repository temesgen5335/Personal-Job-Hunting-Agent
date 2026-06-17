"""Run one ingestion pass with all configured adapters. Hermes cron calls this.

Usage: python scripts/ingest.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobagent.config import get_settings  # noqa: E402
from jobagent.ingestion.adapters.ashby import AshbyAdapter  # noqa: E402
from jobagent.ingestion.adapters.greenhouse import GreenhouseAdapter  # noqa: E402
from jobagent.ingestion.adapters.lever import LeverAdapter  # noqa: E402
from jobagent.ingestion.adapters.remoteok import RemoteOKAdapter  # noqa: E402
from jobagent.ingestion.adapters.remotive import RemotiveAdapter  # noqa: E402
from jobagent.ingestion.adapters.telegram import TelegramAdapter  # noqa: E402
from jobagent.ingestion.runner import run_ingestion  # noqa: E402
from jobagent.ingestion.util import split_slugs  # noqa: E402
from jobagent.preferences import load_preferences  # noqa: E402
from jobagent.store import Store  # noqa: E402


def _merge(*lists) -> list[str]:
    """Union, order-preserving, de-duplicated."""
    seen, out = set(), []
    for lst in lists:
        for s in lst:
            if s and s not in seen:
                seen.add(s)
                out.append(s)
    return out


def build_adapters(settings):
    # Company watchlist comes from config/preferences.toml; env vars supplement it.
    # Free sources run always; ATS adapters gate on having slugs via `enabled`.
    wl = load_preferences().watchlist
    return [
        RemoteOKAdapter(),
        RemotiveAdapter(),
        GreenhouseAdapter(_merge(wl.greenhouse, split_slugs(settings.greenhouse_slugs))),
        LeverAdapter(_merge(wl.lever, split_slugs(settings.lever_slugs))),
        AshbyAdapter(_merge(wl.ashby, split_slugs(settings.ashby_slugs))),
        TelegramAdapter(
            settings.telegram_api_id,
            settings.telegram_api_hash,
            split_slugs(settings.telegram_channels),
            session=settings.telegram_session,
            limit=settings.telegram_fetch_limit,
        ),
    ]


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
