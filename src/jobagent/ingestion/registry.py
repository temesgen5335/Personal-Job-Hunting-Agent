"""Builds the configured set of ingestion adapters from settings + watchlist.
Shared by the ingest CLI and the deployment pipeline so they never drift."""

from __future__ import annotations

from jobagent.ingestion.adapters.ashby import AshbyAdapter
from jobagent.ingestion.adapters.greenhouse import GreenhouseAdapter
from jobagent.ingestion.adapters.lever import LeverAdapter
from jobagent.ingestion.adapters.remoteok import RemoteOKAdapter
from jobagent.ingestion.adapters.remotive import RemotiveAdapter
from jobagent.ingestion.adapters.telegram import TelegramAdapter
from jobagent.ingestion.base import BaseAdapter
from jobagent.ingestion.util import split_slugs
from jobagent.preferences import load_preferences


def _merge(*lists: list[str]) -> list[str]:
    """Union, order-preserving, de-duplicated."""
    seen: set[str] = set()
    out: list[str] = []
    for lst in lists:
        for s in lst:
            if s and s not in seen:
                seen.add(s)
                out.append(s)
    return out


def build_adapters(settings) -> list[BaseAdapter]:
    """Company watchlist + per-source toggles come from config/preferences.toml; env
    vars supplement slugs/channels. A source disabled in [sources] is dropped here;
    enabled ones still self-gate on creds/slugs via their `enabled` property."""
    prefs = load_preferences()
    wl, src = prefs.watchlist, prefs.sources
    all_adapters = [
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
    return [a for a in all_adapters if src.is_enabled(a.source.value)]
