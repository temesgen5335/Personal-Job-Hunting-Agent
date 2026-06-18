"""Tier-1 apply from the terminal.

Prepare assets for a ranked job (generates, does NOT send):
    python scripts/apply.py prepare <rank>          # rank from `scripts/match.py`

Review the printed CV / cover letter / email, then approve to send:
    python scripts/apply.py approve <application_id>

Needs OPENROUTER_API_KEY (to generate) and SMTP_* + APPLY_FROM_EMAIL (to send).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobagent.apply import approve_and_send, load_cv_master, prepare_application  # noqa: E402
from jobagent.bot.service import ranked_matches  # noqa: E402
from jobagent.config import get_settings  # noqa: E402
from jobagent.llm_client import build_llm  # noqa: E402
from jobagent.preferences import load_preferences  # noqa: E402
from jobagent.store import Store  # noqa: E402


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[1] not in {"prepare", "approve"}:
        print(__doc__)
        sys.exit(1)
    cmd, arg = sys.argv[1], sys.argv[2]
    settings = get_settings()
    profile = load_preferences().profile
    store = Store(settings.db_path)
    store.init_schema()

    if cmd == "prepare":
        llm = build_llm(settings)
        if llm is None:
            sys.exit("Set at least one LLM key (GROQ_API_KEY / GEMINI_API_KEY / OPENROUTER_API_KEY / …).")
        ranked = ranked_matches(store, 25)
        try:
            job = ranked[int(arg) - 1]
        except (ValueError, IndexError):
            sys.exit(f"No ranked job #{arg}. Run scripts/match.py to see the list.")
        bundle = prepare_application(store, job, profile, load_cv_master(), llm)
        print(f"\n=== TAILORED CV ===\n{bundle.cv_markdown}\n")
        print(f"=== COVER LETTER ===\n{bundle.cover_letter}\n")
        print(f"=== EMAIL ===\nSubject: {bundle.email_subject}\n\n{bundle.email_body}\n")
        print(f"apply_method: {bundle.apply_method}")
        print(f"\nReview above. To send:  python scripts/apply.py approve {bundle.application_id}")
    else:  # approve
        print(approve_and_send(store, arg, settings, profile))

    store.close()


if __name__ == "__main__":
    main()
