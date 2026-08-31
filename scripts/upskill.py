"""Skill-gap heatmap + a learning plan, from the gaps your matches already recorded.

    python scripts/upskill.py [min_score]      # default min_score = 0.5 (moderate fit)

Prints the recurring skill gaps ranked by fit-weighted frequency. If an LLM key is set,
it also prints a prioritized learning plan (now / next / later). Heuristic-only otherwise.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobagent.config import get_settings  # noqa: E402
from jobagent.llm_client import build_llm  # noqa: E402
from jobagent.preferences import load_preferences  # noqa: E402
from jobagent.store import Store  # noqa: E402
from jobagent.upskill import learning_plan, skill_gaps, structural_notes  # noqa: E402


def _bar(weight: float, peak: float, width: int = 20) -> str:
    filled = round((weight / peak) * width) if peak > 0 else 0
    return "█" * filled + "·" * (width - filled)


def main() -> None:
    min_score = float(sys.argv[1]) if len(sys.argv) > 1 else 0.5
    settings = get_settings()
    profile = load_preferences().profile
    store = Store(settings.db_path)
    store.init_schema()

    matches = store.get_matches(limit=1000, min_score=min_score)
    gaps = skill_gaps(matches, min_score=min_score, limit=20)
    structural = structural_notes(matches, min_score=min_score)

    print(f"\nSkill gaps across {len(matches)} match(es) with fit ≥ {min_score:.0%}\n")
    if not gaps:
        print("  No recurring skill gaps found. Either your matches fit well, or run "
              "scripts/match.py first to score jobs.")
    else:
        peak = gaps[0].weight or 1.0
        for g in gaps:
            print(f"  {g.skill:<28} {_bar(g.weight, peak)}  {g.count:>2} job(s)")
        print("\n  Top exposure:")
        for g in gaps[:5]:
            if g.examples:
                print(f"    • {g.skill}: {', '.join(g.examples)}")

    if structural:
        print("\nNon-skill filters (not upskilling targets):")
        for label, n in sorted(structural.items(), key=lambda kv: kv[1], reverse=True):
            print(f"  {label:<20} {n} job(s)")

    llm = build_llm(settings)
    if llm and gaps:
        print("\n" + "=" * 60 + "\nLEARNING PLAN\n" + "=" * 60)
        skills = ", ".join(profile.core_skills) or "(none listed)"
        print(learning_plan(gaps, skills, llm))
    elif gaps:
        print("\n(Set an LLM key — GROQ_API_KEY / GEMINI_API_KEY / … — for a learning plan.)")

    store.close()


if __name__ == "__main__":
    main()
