"""OpenRouter free-model discovery — stdlib only, so the harness gains it without a
new dependency.

Free model slugs are withdrawn without notice (a pinned `:free` slug 404s the moment it
loses its free tier), so nothing here is hardcoded: it fetches OpenRouter's live catalogue,
keeps the free text-chat models, and ranks them so `build_chain` can fan out over all of
them as failover backends. A dead or gated slug is then simply the next one skipped.

`fetch` is injectable: the default transport is `urllib` (stdlib), and a caller or test
supplies its own to avoid the network entirely. Results are cached per process for an hour;
any failure yields an empty list so the caller falls back to its single configured model.
"""

from __future__ import annotations

import time

MODELS_URL = "https://openrouter.ai/api/v1/models"
_TTL_SECONDS = 3600.0
_cache: dict = {"at": -1e18, "ids": None}


def _default_fetch(url: str, api_key: str, timeout: float) -> dict:
    import json
    import urllib.request
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310 — fixed https URL
        return json.loads(resp.read().decode("utf-8"))


def rank_free(models: list[dict]) -> list[str]:
    """Free `:free` text→text chat models, tool-capable and larger-context first.

    Non-chat entries (audio/image/classifier modalities) and paid models are dropped, so
    every id returned is something a chat/tool backend can actually be pointed at."""
    def is_chat(m: dict) -> bool:
        a = m.get("architecture") or {}
        return ("text" in (a.get("input_modalities") or [])
                and "text" in (a.get("output_modalities") or []))
    free = [m for m in models
            if str(m.get("id", "")).endswith(":free") and is_chat(m)]
    free.sort(key=lambda m: (1 if "tools" in (m.get("supported_parameters") or []) else 0,
                             m.get("context_length") or 0), reverse=True)
    return [str(m["id"]) for m in free]


def free_models(api_key: str = "", *, limit: int = 6, timeout: float = 8.0,
                fetch=None, clock=None) -> list[str]:
    """Live ranked ids of OpenRouter free chat models. Cached for an hour; returns [] on
    any failure so callers degrade to their configured model rather than break."""
    now = (clock or time.monotonic)()
    cached = _cache["ids"]
    if cached is not None and (now - _cache["at"]) < _TTL_SECONDS:
        return cached[:limit]
    try:
        data = (fetch or _default_fetch)(MODELS_URL, api_key, timeout)["data"]
        ids = rank_free(data)
    except Exception:   # noqa: BLE001 — network/parse: resilience, never a hard failure
        ids = []
    _cache.update(at=now, ids=ids)
    return ids[:limit]


def reset_cache() -> None:
    """Drop the cached list (for tests, or to force a refresh)."""
    _cache.update(at=-1e18, ids=None)
