"""Pure bot logic — no telegram import, so it's unit-testable on its own.
The telegram wiring in app.py and the notifier in notify.py call into this.
"""

from __future__ import annotations

from jobagent.digest import diversify, format_matches
from jobagent.store import Store

HELP_TEXT = (
    "🤖 *Personal Job Agent*\n\n"
    "/jobs [N] — top N job matches (default 10)\n"
    "/apply <rank> — draft a tailored application for job #rank from /jobs\n"
    "/status — pipeline stats (jobs, sources, matches)\n"
    "/help — this message\n\n"
    "Daily digests arrive automatically."
)


def is_owner(chat_id: int | None, owner_id: int | None) -> bool:
    """Lock the bot to one chat. If no owner configured, deny everyone (fail closed)."""
    return owner_id is not None and chat_id == owner_id


def ranked_matches(store: Store, n: int = 10) -> list[dict]:
    """The diversified, ranked shortlist. /jobs and /apply <rank> share this so the
    numbering a user sees maps to the right job."""
    pool = store.get_top_matches(limit=max(n * 8, 40), min_score=0.0)
    return diversify(pool, n, max_per_company=2)


def jobs_text(store: Store, n: int = 10) -> str:
    return format_matches(ranked_matches(store, n))


def resolve_ranked_job(store: Store, rank: int, pool: int = 25) -> dict | None:
    """Map a 1-based rank (as shown by /jobs) to its job dict, or None if out of range."""
    ranked = ranked_matches(store, pool)
    if rank < 1 or rank > len(ranked):
        return None
    return ranked[rank - 1]


def apply_callback_data(action: str, app_id: str) -> str:
    return f"{action}:{app_id}"


def parse_callback_data(data: str) -> tuple[str, str]:
    action, _, app_id = (data or "").partition(":")
    return action, app_id


def _truncate(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n].rstrip() + "…"


def apply_preview_text(bundle) -> str:
    """Telegram-friendly review of a drafted application (CV is sent separately as a file)."""
    job = bundle.job
    lines = [
        f"📝 *Draft application* — {job.get('title')} @ {job.get('company')}",
        f"Method: {bundle.apply_method}",
        "",
        f"✉️ *Subject:* {bundle.email_subject}",
        _truncate(bundle.email_body, 800),
        "",
        "📄 *Cover letter:*",
        _truncate(bundle.cover_letter, 900),
        "",
        f"📎 Tailored CV ({len(bundle.cv_markdown)} chars) attached above.",
    ]
    if bundle.apply_method != "email":
        lines.append("\n⚠️ Not an email posting — approving hands you the apply link (Tier-2 form-fill is Phase 4).")
    lines.append("\nApprove to send?")
    return "\n".join(lines)


def status_text(stats: dict) -> str:
    by_source = stats.get("by_source", {})
    src_lines = "\n".join(f"   • {s}: {n}" for s, n in by_source.items()) or "   (none)"
    last = stats.get("last_ingest") or "never"
    return (
        "📊 *Pipeline status*\n"
        f"Total jobs: {stats.get('total_jobs', 0)}\n"
        f"Scored matches: {stats.get('matches', 0)} "
        f"(strong ≥70%: {stats.get('strong_matches', 0)})\n"
        f"Last ingest: {last}\n"
        f"By source:\n{src_lines}"
    )
