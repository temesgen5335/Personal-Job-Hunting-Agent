"""Pure ATS form-mapping: detect platform, build the field plan + applicant info.

No Playwright here — this is all testable. The driver (executor.py) consumes the plan.
Selectors are best-effort candidate lists (tried in order); the screenshot + HITL gate
is what guarantees correctness, since ATS DOMs drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jobagent.preferences import Profile

# Detect a *rendered* challenge element, not a substring in bundled JS (which would
# false-positive on any site that merely ships these libraries).
CAPTCHA_SELECTORS = (
    "iframe[src*=recaptcha]", ".g-recaptcha", "#g-recaptcha",
    "iframe[src*=hcaptcha]", ".h-captcha",
    ".cf-turnstile", "iframe[src*='challenges.cloudflare.com']",
)

SUBMIT_SELECTORS = {
    "greenhouse": ["#submit_app", "button#submit_app", "button[type=submit]"],
    "lever": ["button[type=submit]", "button.template-btn-submit"],
    "ashby": ["button[type=submit]"],
}


@dataclass
class ApplicantInfo:
    full_name: str
    first_name: str
    last_name: str
    email: str
    phone: str
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""
    cv_path: str = ""

    @classmethod
    def from_profile(cls, profile: Profile) -> "ApplicantInfo":
        parts = (profile.name or "").split()
        first = parts[0] if parts else ""
        last = " ".join(parts[1:]) if len(parts) > 1 else ""
        links = profile.links or {}
        return cls(
            full_name=profile.name, first_name=first, last_name=last,
            email=profile.email, phone=profile.phone,
            linkedin=links.get("linkedin", ""), github=links.get("github", ""),
            portfolio=links.get("portfolio", ""), cv_path=profile.cv_path,
        )


@dataclass
class FieldAction:
    label: str
    selectors: list[str]
    value: str | None = None
    file: str | None = None
    kind: str = "fill"        # "fill" | "upload"


def detect_platform(url: str) -> str | None:
    u = (url or "").lower()
    if "greenhouse.io" in u or "gh_jid=" in u:   # gh_jid = Greenhouse behind a custom domain
        return "greenhouse"
    if "lever.co" in u:
        return "lever"
    if "ashbyhq.com" in u:
        return "ashby"
    return None


def apply_target(job: dict) -> tuple[str | None, str]:
    """Resolve (platform, fillable_url) for a job. Trusts the job's `source` (we know
    which adapter ingested it) over URL sniffing, and rewrites company career-site
    redirects to the canonical ATS-hosted form. Falls back to URL detection."""
    source = (job.get("source") or "").lower()
    url = job.get("apply_url") or job.get("url") or ""
    slug = job.get("company") or ""
    jid = job.get("source_job_id") or ""

    if source == "greenhouse":
        if "greenhouse.io" in url.lower():
            return "greenhouse", url
        if slug and jid:  # e.g. instacart.careers/?gh_jid=… → canonical GH form
            return "greenhouse", f"https://job-boards.greenhouse.io/{slug}/jobs/{jid}"
        return "greenhouse", url
    if source in ("lever", "ashby"):
        return source, url  # adapter already stored the hosted application URL

    return detect_platform(url), url


def field_plan(platform: str, a: ApplicantInfo) -> list[FieldAction]:
    if platform == "greenhouse":
        return [
            FieldAction("First name", ["#first_name"], value=a.first_name),
            FieldAction("Last name", ["#last_name"], value=a.last_name),
            FieldAction("Email", ["#email", "input[type=email]"], value=a.email),
            FieldAction("Phone", ["#phone", "input[type=tel]"], value=a.phone),
            FieldAction("Resume", ["input#resume", "input[type=file]"], file=a.cv_path, kind="upload"),
        ]
    if platform == "lever":
        return [
            FieldAction("Name", ["input[name=name]"], value=a.full_name),
            FieldAction("Email", ["input[name=email]", "input[type=email]"], value=a.email),
            FieldAction("Phone", ["input[name=phone]", "input[type=tel]"], value=a.phone),
            FieldAction("LinkedIn", ["input[name='urls[LinkedIn]']"], value=a.linkedin),
            FieldAction("GitHub", ["input[name='urls[GitHub]']"], value=a.github),
            FieldAction("Resume", ["input[name=resume]", "input[type=file]"], file=a.cv_path, kind="upload"),
        ]
    if platform == "ashby":
        return [
            FieldAction("Name", ["input[name=_systemfield_name]", "input[name=name]"], value=a.full_name),
            FieldAction("Email", ["input[name=_systemfield_email]", "input[type=email]"], value=a.email),
            FieldAction("Phone", ["input[type=tel]"], value=a.phone),
            FieldAction("Resume", ["input[type=file]"], file=a.cv_path, kind="upload"),
        ]
    return []
