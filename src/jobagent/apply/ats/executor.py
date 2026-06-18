"""ATS form driver. The pure-ish `execute()` works against any page-like object
(Playwright Page in prod, a fake in tests). `apply_to_job()` launches a real browser.

Guarantees:
- R2: submits ONLY when submit=True and a submit control is found.
- R3: if a CAPTCHA/anti-bot challenge is present, never submit — screenshot and stop;
  the caller hands the user the link to finish manually.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jobagent.apply.ats.fields import (
    CAPTCHA_SELECTORS,
    SUBMIT_SELECTORS,
    ApplicantInfo,
    FieldAction,
    field_plan,
)


@dataclass
class ExecResult:
    platform: str
    url: str = ""
    filled: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    captcha_detected: bool = False
    submitted: bool = False
    screenshot_path: str | None = None

    def summary(self) -> str:
        parts = [f"Platform: {self.platform}", f"Filled: {', '.join(self.filled) or '—'}"]
        if self.missing:
            parts.append(f"Not found: {', '.join(self.missing)}")
        if self.captcha_detected:
            parts.append("⚠️ CAPTCHA detected — submit blocked; finish manually.")
        parts.append("✅ Submitted." if self.submitted else "⏸ Filled, NOT submitted (awaiting approval).")
        return "\n".join(parts)


def _first_present(page, selectors: list[str]) -> str | None:
    for sel in selectors:
        if page.query_selector(sel) is not None:
            return sel
    return None


def _has_captcha(page) -> bool:
    """True only if a rendered CAPTCHA element is on the page."""
    return any(page.query_selector(sel) is not None for sel in CAPTCHA_SELECTORS)


def execute(page, platform: str, plan: list[FieldAction], screenshot_path: str, submit: bool = False) -> ExecResult:
    result = ExecResult(platform=platform, screenshot_path=screenshot_path)
    for action in plan:
        if action.kind == "fill" and not action.value:
            continue                      # nothing to type (e.g. no GitHub on file)
        if action.kind == "upload" and not action.file:
            continue
        sel = _first_present(page, action.selectors)
        if sel is None:
            result.missing.append(action.label)
            continue
        if action.kind == "upload":
            page.set_input_files(sel, action.file)
        else:
            page.fill(sel, action.value)
        result.filled.append(action.label)

    result.captcha_detected = _has_captcha(page)
    try:
        page.screenshot(path=screenshot_path)
    except Exception:  # noqa: BLE001 — heavy SPAs can stall the screenshot; degrade, don't crash
        result.screenshot_path = None

    # R2 + R3: only submit on explicit request AND when no CAPTCHA blocks us.
    if submit and not result.captcha_detected:
        sel = _first_present(page, SUBMIT_SELECTORS.get(platform, []))
        if sel is not None:
            page.click(sel)
            result.submitted = True
    return result


def apply_to_job(platform: str, url: str, applicant: ApplicantInfo, screenshot_path: str,
                 submit: bool = False, headless: bool = True) -> ExecResult:
    """Launch a real browser, fill the form, screenshot, optionally submit.
    `platform` is resolved by fields.apply_target (which trusts the job's source)."""
    if platform not in SUBMIT_SELECTORS:
        raise ValueError(f"Unsupported ATS platform: {platform!r}")
    plan = field_plan(platform, applicant)

    from playwright.sync_api import sync_playwright  # lazy: optional [apply] extra

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        page.set_default_timeout(15000)  # don't hang forever on heavy SPAs
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            # ATS forms are SPAs — give fields time to render before we look for them.
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001 — best effort; proceed and report missing fields
                pass
            result = execute(page, platform, plan, screenshot_path, submit=submit)
            result.url = url
            return result
        finally:
            browser.close()
