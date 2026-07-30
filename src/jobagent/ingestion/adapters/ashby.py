"""Ashby public job-board API.

GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
→ {"jobs": [{id, title, location, isRemote, employmentType, descriptionPlain,
             descriptionHtml, applyUrl, jobUrl, publishedAt, compensation, ...}]}
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

import httpx

from jobagent.core.schemas import ApplyMethod, JobPosting, Source
from jobagent.ingestion.base import BaseAdapter
from jobagent.ingestion.util import get_with_retry, make_client, strip_html


class AshbyAdapter(BaseAdapter):
    source = Source.ashby
    BASE = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"

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
                    continue
                for item in jobs:
                    if isinstance(item, dict):
                        yield self._normalize(item, slug)
        finally:
            if owns:
                client.close()

    def _normalize(self, item: dict, slug: str) -> JobPosting:
        return JobPosting(
            source=Source.ashby,
            source_job_id=str(item.get("id")),
            title=item.get("title") or "(untitled)",
            company=slug,
            location=item.get("location"),
            is_remote=bool(item.get("isRemote")),
            description=item.get("descriptionPlain") or strip_html(item.get("descriptionHtml")),
            salary_text=_comp(item.get("compensation")),
            apply_method=ApplyMethod.ats_form,  # Ashby-hosted form → Tier 2
            apply_url=item.get("applyUrl") or item.get("jobUrl"),
            url=item.get("jobUrl"),
            posted_at=_iso(item.get("publishedAt")),
            tags=[t for t in [item.get("employmentType"), item.get("team")] if t],
            raw=item,
        )


def _comp(comp) -> str | None:
    if isinstance(comp, dict):
        return comp.get("compensationTierSummary") or None
    return None


def _iso(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
