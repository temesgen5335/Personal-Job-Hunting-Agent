"""Print the matching eval table — the tuning loop for the heuristic scorer.

Usage:
    .venv/bin/python scripts/eval_matching.py

Change the scorer, run this, and watch the metrics + which traps move. The floors in
tests/test_eval_matching.py fail CI on regression; this script is how you see *why*.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jobagent.matching.evalset import evaluate  # noqa: E402


def main() -> None:
    m = evaluate()
    print(f"examples: {m['n']} ({m['n_pos']} relevant)")
    print(f"precision@5 : {m['precision_at_5']:.2f}")
    print(f"precision@10: {m['precision_at_10']:.2f}")
    print(f"recall@10   : {m['recall_at_10']:.2f}")
    print(f"mean pos    : {m['mean_pos']:.3f}")
    print(f"mean neg    : {m['mean_neg']:.3f}")
    print(f"separation  : {m['separation']:.3f}")
    print("\nranked (✓ relevant / ✗ irrelevant):")
    for r in m["rows"]:
        mark = "✓" if r["relevant"] else "✗"
        gaps = f"  ⚠ {'; '.join(r['gaps'])}" if r["gaps"] else ""
        print(f"  {r['score']:.3f} {mark} {r['title'][:44]:<44} {r['note'][:44]}{gaps}")


if __name__ == "__main__":
    main()
