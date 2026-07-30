"""Greenhouse public job-board API.

GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
→ {"jobs": [{id, title, location:{name}, content (HTML-escaped), absolute_url,
            updated_at, metadata}]}. `content=true` is required to get descriptions.
"""

from __future__ import annotations

import html
from collections.abc import Iterable
from datetime import datetime

import httpx

from jobagent.core.schemas import ApplyMethod, JobPosting, Source
from jobagent.ingestion.base import BaseAdapter
from jobagent.ingestion.util import get_with_retry, make_client, strip_html


class GreenhouseAdapter(BaseAdapter):
    source = Source.greenhouse
    BASE = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"

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
                    jobs = get_with_retry(client, self.BASE.format(slug=slug)).json().get("jobs", [])
                except (httpx.HTTPError, ValueError):
                    continue  # bad/unknown slug — skip, keep the rest
                for item in jobs:
                    if isinstance(item, dict):
                        yield self._normalize(item, slug)
        finally:
            if owns:
                client.close()

    def _normalize(self, item: dict, slug: str) -> JobPosting:
        return JobPosting(
            source=Source.greenhouse,
            source_job_id=str(item.get("id")),
            title=item.get("title") or "(untitled)",
            company=slug,  # board slug; human company name often == slug
            location=(item.get("location") or {}).get("name"),
            is_remote=_looks_remote(item),
            description=strip_html(html.unescape(item.get("content", ""))),
            apply_method=ApplyMethod.ats_form,  # Greenhouse-hosted form → Tier 2
            apply_url=item.get("absolute_url"),
            url=item.get("absolute_url"),
            posted_at=_iso(item.get("updated_at")),
            raw=item,
        )


def _looks_remote(item: dict) -> bool:
    loc = ((item.get("location") or {}).get("name") or "").lower()
    return "remote" in loc


def _iso(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
