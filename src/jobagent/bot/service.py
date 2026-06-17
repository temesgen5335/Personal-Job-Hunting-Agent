"""Pure bot logic — no telegram import, so it's unit-testable on its own.
The telegram wiring in app.py and the notifier in notify.py call into this.
"""

from __future__ import annotations

from jobagent.digest import format_digest
from jobagent.store import Store

HELP_TEXT = (
    "🤖 *Personal Job Agent*\n\n"
    "/jobs [N] — top N job matches (default 10)\n"
    "/status — pipeline stats (jobs, sources, matches)\n"
    "/help — this message\n\n"
    "Daily digests arrive automatically."
)


def is_owner(chat_id: int | None, owner_id: int | None) -> bool:
    """Lock the bot to one chat. If no owner configured, deny everyone (fail closed)."""
    return owner_id is not None and chat_id == owner_id


def jobs_text(store: Store, n: int = 10) -> str:
    # Wide pool so the per-company diversity cap in the digest has room to choose.
    pool = store.get_top_matches(limit=max(n * 8, 40), min_score=0.0)
    return format_digest(pool, limit=n, max_per_company=2)


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
