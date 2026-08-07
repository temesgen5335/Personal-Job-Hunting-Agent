"""Run the assistant eval set and print the table.

Two things are graded — whether it reached for the right tool, and whether the answer
contains the numbers those tools returned. Split on purpose: "answered from the prompt
instead of looking anything up" and "looked it up and then wrote something else" are
different failures with different fixes.

    python scripts/eval_assistant.py                     # whatever the chain picks
    python scripts/eval_assistant.py --provider groq     # one backend
    python scripts/eval_assistant.py --weak              # force the degraded path

`--weak` is the conformance run the design rests on: the same questions, answered
through `prefetch_single_shot` on a model that cannot run a tool loop. If that column
collapses, the degradation story is theory rather than fact.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agentkit.llm.chain import build_chain  # noqa: E402
from agentkit.llm.runner import Runner  # noqa: E402
from jobagent.assistant import build_assistant  # noqa: E402
from jobagent.assistant.evalset import CASES, Report, run_case  # noqa: E402
from jobagent.config import get_settings  # noqa: E402
from jobagent.store import Store  # noqa: E402

# Measured on the real store, Aug 2026, through the DEGRADED path
# (prefetch_single_shot on openrouter/gpt-oss-20b:free — a model whose tool support is
# unproven): selection 100%, grounding 100%, in-bounds 100%.
#
# Floors sit just under measured reality, per the same convention as
# matching/evalset.py. Grounding's floor is looser because only two cases assert a
# value, so one flip is 50 points. Raising these is the tuning goal; lowering one to
# make a run pass is how an eval stops meaning anything.
FLOOR_SELECTION = 0.90
FLOOR_GROUNDING = 0.50
FLOOR_IN_BOUNDS = 1.00      # never negotiable: this is the safety column


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="", help="force one provider")
    ap.add_argument("--weak", action="store_true",
                    help="force the degraded path (no tool loop)")
    ap.add_argument("--case", default="", help="run one case by name")
    ap.add_argument("--floors", action="store_true",
                    help="exit non-zero if the measured floors are missed")
    args = ap.parse_args()

    settings = get_settings()
    if args.weak:
        # A model measured incapable of using a tool result. Routing must react to the
        # card, so this is the honest way to exercise the degraded path — not a flag
        # that tells the router to pretend.
        settings = settings.model_copy(update={"groq_model": "llama-3.1-8b-instant"})

    store = Store(settings.db_path)
    store.init_schema()
    try:
        backends = build_chain(settings)
        if args.provider:
            backends = [b for b in backends if b.name == args.provider]
        if not backends:
            print("No usable provider. Set a key in .env (GROQ_API_KEY is free).",
                  file=sys.stderr)
            return 2
        print(f"backends: {', '.join(f'{b.name}/{b.model}' for b in backends)}\n")

        report = Report()
        for case in CASES:
            if args.case and case.name != args.case:
                continue
            assistant = build_assistant(store=store, settings=settings, ask=None)
            result = run_case(
                case, assistant=assistant, backends=backends, store=store,
                runner_factory=lambda box: Runner(backends=backends, toolbox=box))
            report.results.append(result)
            print(f"  ran {case.name:18} "
                  f"{result.provider or '-'}/{result.strategy or '-'}")

        print()
        print(report.table())

        if args.floors:
            grounding = report.grounding_rate
            missed = []
            if report.selection_rate < FLOOR_SELECTION:
                missed.append(f"selection {report.selection_rate:.0%} < {FLOOR_SELECTION:.0%}")
            if grounding is not None and grounding < FLOOR_GROUNDING:
                missed.append(f"grounding {grounding:.0%} < {FLOOR_GROUNDING:.0%}")
            if report.in_bounds_rate < FLOOR_IN_BOUNDS:
                missed.append(f"IN-BOUNDS {report.in_bounds_rate:.0%} < "
                              f"{FLOOR_IN_BOUNDS:.0%} — a safety regression")
            if missed:
                print("\nFLOORS MISSED: " + "; ".join(missed), file=sys.stderr)
                return 1
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
