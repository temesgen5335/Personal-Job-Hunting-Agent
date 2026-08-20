"""Explain the LLM setup: what is configured, what each model can do, and why.

Read-only and offline by default — it makes no API calls unless you pass `--probe`.
The point is to turn "why did it use the slow model?" from a mystery into a paragraph
you can read, because the routing deliberately does *not* obey `LLM_PROVIDER`: a primary
that cannot serve a task is skipped, which looks like a bug the first time you see it.

    python scripts/llm_doctor.py                # chain, cards, and routing per task
    python scripts/llm_doctor.py --probe        # one tiny live call per backend
    python scripts/llm_doctor.py --task assistant_answer
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agentkit.llm.capabilities import Tier  # noqa: E402
from agentkit.llm.chain import DEFAULT_ORDER, build_chain  # noqa: E402
from agentkit.llm.errors import classify  # noqa: E402
from agentkit.llm.router import plans_for  # noqa: E402
from agentkit.llm.tasks import STEP_BUDGET, Budget, TaskSpec  # noqa: E402
from agentkit.llm.types import ChatRequest, Message, ToolSpec  # noqa: E402
from jobagent.config import get_settings  # noqa: E402

# The shapes of work this system actually asks for. Each one is a real caller, so a
# task that cannot route here is a feature that will silently degrade in production.
PROBE_TOOL = ToolSpec("lookup", "Look something up.", {
    "type": "object",
    "properties": {"what": {"type": "string", "description": "what to look up"}},
    "required": ["what"]})

TASKS = (
    TaskSpec("scoring", needs_json=True, min_tier=Tier.TINY,
             est_input_tokens=1500),
    TaskSpec("fit_check", needs_json=True, min_tier=Tier.WEAK,
             est_input_tokens=4000),
    TaskSpec("email_draft", needs_json=True, min_tier=Tier.WEAK,
             est_input_tokens=6000),
    TaskSpec("assistant_answer", needs_tools=True, max_tool_steps=5,
             tools=(PROBE_TOOL,), min_tier=Tier.WEAK, est_input_tokens=3000,
             prefetch=lambda inputs, toolbox: "", budget=Budget()),
    TaskSpec("assistant_action", needs_tools=True, max_tool_steps=1,
             needs_synthesis=False, tools=(PROBE_TOOL,), min_tier=Tier.WEAK),
)


def rule(label: str = "") -> None:
    print(f"\n{'─' * 4} {label} {'─' * max(0, 66 - len(label))}" if label else "─" * 72)


def show_chain(report) -> None:
    rule("chain")
    if not report.backends:
        print("  (no usable provider — every one was skipped)")
    for i, b in enumerate(report.backends):
        card = b.card
        loop = {True: "yes", False: "no", None: "unproven"}[card.tool_loop]
        tools = {True: "yes", False: "no", None: "unproven"}[card.native_tools]
        print(f"  {i}. {b.name}/{b.model}")
        print(f"       tier {card.tier.name.lower():<8} "
              f"context {card.context_tokens or '?':<9} "
              f"tools {tools:<9} loop {loop}")
        print(f"       source: {card.source}"
              + (f" — {card.notes}" if card.notes else ""))
    for name, why in report.skipped:
        print(f"  -  {name}: {why}")


def show_routing(report, only: str = "") -> None:
    rule("routing")
    print("  The configured primary only breaks ties. Admission is per task, so a\n"
          "  primary that cannot do the job is skipped — with a reason.\n")
    for task in TASKS:
        if only and task.name != only:
            continue
        plans, rejections = plans_for(task, report.backends)
        need = [f"tier≥{task.min_tier.name.lower()}"]
        if task.needs_tools:
            need.append(f"{task.max_tool_steps} tool step(s)")
        if task.needs_json:
            need.append("json")
        print(f"  {task.name}  ({', '.join(need)})")
        if plans:
            for p in plans:
                flag = "" if p.level == 0 else f"  ← degraded (level {p.level})"
                print(f"      → {p.backend.name}/{p.backend.model}: {p.strategy}{flag}")
        else:
            print("      → nothing can run this task")
        for r in rejections:
            print(f"        skipped {r}")
        print()


def show_budgets() -> None:
    rule("step budgets by tier")
    for tier, steps in STEP_BUDGET.items():
        note = "  (emits calls but cannot use a result — measured)" \
            if tier is Tier.WEAK else ""
        print(f"  {tier.name.lower():<9} {steps} step(s){note}")


def probe(report) -> None:
    """One tiny live call per backend, then one realistically-sized one.

    The tiny call alone is misleading, and that was observed rather than predicted:
    with Groq's tokens-per-day budget nearly spent, "Reply with just: ok" fits and
    reports *reachable*, while a real assistant turn — a system prompt plus fourteen
    tool schemas — does not. A diagnostic that says a provider is fine when it cannot
    serve a request is worse than no diagnostic, because it is consulted precisely when
    something is already wrong. So both are measured, and both are shown.
    """
    rule("live probe")
    filler = "context " * 400        # ~1.5k tokens: the size a real task actually sends
    for b in report.backends:
        try:
            r = b.chat(ChatRequest(messages=[Message("user", "Reply with just: ok")],
                                   max_tokens=8, timeout_s=25))
            tiny = f"reachable — {r.text.strip()[:30]!r}"
        except Exception as exc:  # noqa: BLE001 — reporting on failure is the point
            c = classify(exc)
            print(f"  {b.name}/{b.model}: {c.verdict} — {c.message[:100]}")
            continue

        try:
            b.chat(ChatRequest(system="You are a diagnostic.",
                               messages=[Message("user", f"{filler}\nReply: ok")],
                               max_tokens=8, timeout_s=30))
            print(f"  {b.name}/{b.model}: {tiny}; a full-size request also fits")
        except Exception as exc:  # noqa: BLE001
            c = classify(exc)
            print(f"  {b.name}/{b.model}: {tiny}")
            print(f"       ...but a REAL-SIZED request fails: {c.verdict} — "
                  f"{c.message[:90]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--health", action="store_true",
                    help="concurrent pre-flight: one small live call per backend, "
                         "then a health table (fast — seconds, not minutes)")
    ap.add_argument("--probe", action="store_true",
                    help="make one tiny live call per backend (spends quota)")
    ap.add_argument("--task", default="", help="explain routing for one task only")
    args = ap.parse_args()

    settings = get_settings()
    report = build_chain(settings, report=True)

    print("LLM doctor — read-only" + (" (with live probe)" if args.probe else ""))
    print(f"  LLM_PROVIDER = {getattr(settings, 'llm_provider', '') or '(unset)'}"
          f"   fallback order: {' → '.join(DEFAULT_ORDER)}")

    show_chain(report)
    show_routing(report, args.task)
    show_budgets()
    if args.health:
        # Concurrent and cheap. The sequential --probe below is the DEEP check (it also
        # sends a realistically-sized request); this one answers "which of my keys work
        # right now" in the time of the slowest single provider rather than the sum.
        from agentkit.llm.ledger import Ledger
        from agentkit.llm.probe import probe_all

        rule("pre-flight (concurrent)")
        ledger = Ledger()
        print(probe_all(report.backends, ledger=ledger, timeout_s=30).render())
        rule("provider health")
        print(ledger.render())

    if args.probe:
        probe(report)

    if not report.backends:
        print("\nNothing is configured. Set GROQ_API_KEY (free) in .env to start.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
