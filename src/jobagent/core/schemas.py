"""Core domain schemas — the common contract every layer speaks.

Every ingestion adapter normalizes its source into `JobPosting`. Matching produces
`Match`. The apply pipeline produces `Application` + `CVVariant`. `Event` is the
append-only audit trail. Keep these stable; adapters and tools depend on them.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Source(str, Enum):
    """Where a posting came from. One enum value per ingestion adapter."""

    remoteok = "remoteok"
    remotive = "remotive"
    greenhouse = "greenhouse"
    lever = "lever"
    ashby = "ashby"
    telegram = "telegram"
    aggregator = "aggregator"  # SerpApi/Apify → Indeed/LinkedIn/Glassdoor/JobRight
    scrape = "scrape"          # Playwright fallback


class ApplyMethod(str, Enum):
    email = "email"            # Tier 1: draft + send email
    ats_form = "ats_form"      # Tier 2: HITL browser form-fill (GH/Lever/Ashby)
    external_link = "external_link"  # hand off to user with a deep link
    unknown = "unknown"


class ApplicationStatus(str, Enum):
    """Forward-leaning lifecycle for a single application (mirrors HITL gating)."""

    matched = "matched"          # surfaced to user, no action yet
    drafting = "drafting"        # assets being generated
    awaiting_approval = "awaiting_approval"  # HITL gate — nothing sent yet
    submitted = "submitted"
    rejected = "rejected"
    interview = "interview"
    offer = "offer"
    skipped = "skipped"          # user declined
    failed = "failed"            # automation could not complete


class JobPosting(BaseModel):
    """Normalized job posting. The dedup_hash collapses the same role seen on
    multiple sources into one logical job."""

    model_config = ConfigDict(use_enum_values=True)

    id: str | None = None  # store-assigned (dedup_hash) once persisted
    source: Source
    source_job_id: str | None = None  # native id within the source, if any
    title: str
    company: str | None = None
    location: str | None = None
    is_remote: bool = False
    description: str = ""
    salary_text: str | None = None
    apply_method: ApplyMethod = ApplyMethod.unknown
    apply_url: str | None = None
    apply_email: str | None = None
    url: str | None = None  # canonical posting URL
    posted_at: datetime | None = None
    fetched_at: datetime = Field(default_factory=_utcnow)
    tags: list[str] = Field(default_factory=list)
    raw: dict = Field(default_factory=dict)  # full source payload — never discard

    def dedup_hash(self) -> str:
        """Stable identity across sources: normalized company+title+location."""
        basis = "|".join(
            (self.company or "").strip().lower().split()
            + (self.title or "").strip().lower().split()
            + (self.location or "").strip().lower().split()
        )
        return hashlib.sha256(basis.encode()).hexdigest()[:16]


class Match(BaseModel):
    """Heuristic/LLM assessment of one job against the user's profile."""

    model_config = ConfigDict(use_enum_values=True)

    job_id: str
    score: float = Field(ge=0.0, le=1.0)  # 0..1 fit
    rationale: str = ""                   # why it fits
    gaps: list[str] = Field(default_factory=list)  # missing requirements
    created_at: datetime = Field(default_factory=_utcnow)


class CVVariant(BaseModel):
    """A CV tailored to a specific job. HARD RULE: reframes real experience only,
    never fabricates. `base_cv_id` tracks provenance back to the master CV."""

    model_config = ConfigDict(use_enum_values=True)

    id: str | None = None
    job_id: str
    base_cv_id: str
    content_markdown: str
    notes: str = ""  # what was emphasized/reordered and why
    created_at: datetime = Field(default_factory=_utcnow)


class Application(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    id: str | None = None
    job_id: str
    status: ApplicationStatus = ApplicationStatus.matched
    cv_variant_id: str | None = None
    cover_letter: str | None = None
    email_draft: str | None = None
    apply_method: ApplyMethod = ApplyMethod.unknown
    approved_at: datetime | None = None  # HITL gate stamp — set only on user approval
    submitted_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Event(BaseModel):
    """Append-only audit line. Every state change and external action logs one."""

    model_config = ConfigDict(use_enum_values=True)

    id: int | None = None
    kind: str            # e.g. "ingest", "match", "approve", "submit", "error"
    job_id: str | None = None
    payload: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_utcnow)
