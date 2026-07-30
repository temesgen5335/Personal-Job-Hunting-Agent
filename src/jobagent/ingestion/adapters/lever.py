"""Lever public postings API.

GET https://api.lever.co/v0/postings/{slug}?mode=json
→ [ {id, text(title), categories:{location,team,commitment}, descriptionPlain,
     hostedUrl, applyUrl, createdAt(ms epoch), ...}, ... ]
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

import httpx

from jobagent.core.schemas import ApplyMethod, JobPosting, Source
from jobagent.ingestion.base import BaseAdapter
from jobagent.ingestion.util import get_with_retry, make_client, strip_html


class LeverAdapter(BaseAdapter):
    source = Source.lever
    BASE = "https://api.lever.co/v0/postings/{slug}?mode=json"

    def __init__(self, slugs: list[str], client: httpx.Client | None = None):
        self.slugs = slugs
        self._client = client

    @property
    def enabled(self) -> bool:
        return bool(self.slugs)

    def fetch(self) -> Iterable[JobPosting]:
        client, owns = make_client(self._client)
        try:
            for slug in self.slugs:
                try:
                    postings = get_with_retry(client, self.BASE.format(slug=slug)).json()
                except (httpx.HTTPError, ValueError):
                    continue
                for item in postings if isinstance(postings, list) else []:
                    if isinstance(item, dict):
                        yield self._normalize(item, slug)
        finally:
            if owns:
                client.close()

    def _normalize(self, item: dict, slug: str) -> JobPosting:
        cats = item.get("categories") or {}
        location = cats.get("location")
        return JobPosting(
            source=Source.lever,
            source_job_id=str(item.get("id")),
            title=item.get("text") or "(untitled)",
            company=slug,
            location=location,
            is_remote="remote" in (location or "").lower(),
            description=item.get("descriptionPlain") or strip_html(item.get("description")),
            apply_method=ApplyMethod.ats_form,  # Lever-hosted form → Tier 2
            apply_url=item.get("applyUrl") or item.get("hostedUrl"),
            url=item.get("hostedUrl"),
            posted_at=_ms_epoch(item.get("createdAt")),
            tags=[t for t in [cats.get("team"), cats.get("commitment")] if t],
            raw=item,
        )


def _ms_epoch(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc) if value else None
    except (ValueError, TypeError, OSError):
        return None
