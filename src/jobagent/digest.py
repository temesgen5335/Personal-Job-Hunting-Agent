"""Format the top matches into a human-readable shortlist. Used by the match CLI
now and the Telegram bot in the next step."""

from __future__ import annotations

import json


def _diversify(matches: list[dict], limit: int, max_per_company: int) -> list[dict]:
    """Cap how many roles from one company appear, so a single employer with many
    near-identical openings doesn't flood the shortlist."""
    seen: dict[str, int] = {}
    out = []
    for m in matches:
        key = (m.get("company") or m.get("source") or "").lower()
        if seen.get(key, 0) >= max_per_company:
            continue
        seen[key] = seen.get(key, 0) + 1
        out.append(m)
        if len(out) >= limit:
            break
    return out


def format_digest(matches: list[dict], limit: int = 10, max_per_company: int = 2) -> str:
    if not matches:
        return "No matches yet. Run ingestion + matching first."
    matches = _diversify(matches, limit, max_per_company)
    lines = [f"🎯 Top {len(matches)} job matches\n"]
    for i, m in enumerate(matches, 1):
        pct = int(round(m["score"] * 100))
        company = m.get("company") or m.get("source")
        loc = m.get("location") or ("Remote" if m.get("is_remote") else "—")
        link = m.get("apply_url") or m.get("url") or ""
        try:
            gaps = json.loads(m.get("gaps") or "[]")
        except (json.JSONDecodeError, TypeError):
            gaps = []
        lines.append(f"{i}. [{pct}%] {m['title']} — {company} ({loc})")
        if m.get("rationale"):
            lines.append(f"    ↳ {m['rationale']}")
        if gaps:
            lines.append(f"    ⚠ {'; '.join(gaps)}")
        if link:
            lines.append(f"    {link}")
    return "\n".join(lines)
