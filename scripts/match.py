"""Score all stored jobs against the profile and print the top shortlist.

Heuristic-only if no OPENROUTER_API_KEY; LLM-reranks the top candidates if set.
Usage: python scripts/match.py [N]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobagent.config import get_settings  # noqa: E402
from jobagent.digest import format_digest  # noqa: E402
from jobagent.matching import run_matching  # noqa: E402
from jobagent.preferences import load_preferences  # noqa: E402
from jobagent.store import Store  # noqa: E402


def main() -> None:
    top_n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    settings = get_settings()
    profile = load_preferences().profile
    store = Store(settings.db_path)
    store.init_schema()

    report = run_matching(
        store, profile, openrouter_key=settings.openrouter_api_key, model=settings.llm_model
    )
    mode = "heuristic + LLM rerank" if report.used_llm else "heuristic only (no OPENROUTER_API_KEY)"
    print(f"Scored {report.scored} jobs ({mode}); LLM-reranked {report.llm_reranked}.\n")
    # Fetch a wide pool so per-company diversification has candidates to choose from.
    pool = store.get_top_matches(limit=top_n * 8, min_score=0.0)
    print(format_digest(pool, limit=top_n, max_per_company=2))
    store.close()


if __name__ == "__main__":
    main()
