"""How each strategy actually runs a task.

One signature for all of them — `(task, backend, toolbox, inputs, budget) -> str` — so
the Runner can swap execution paths without knowing which it picked. That is the whole
point of the capability routing: the same task produces the same kind of answer on a
frontier model and on a 7B one, by changing *how* rather than *what*.

The load-bearing one is `prefetch_single_shot`. When a model cannot carry state across
a tool result, the fix is not a cleverer prompt — it is to stop asking it to. Python
runs the plan deterministically and the model only writes the answer. This generalizes a
pattern the host project already proved: compute what is computable, and leave the model
the judgement.
"""

from __future__ import annotations

import time

from agentkit.llm import jsonx
from agentkit.llm.tasks import Budget, Strategy
from agentkit.llm.types import ChatRequest, Message, ToolCall

# Tool emulation for backends whose native support is unproven. Kept blunt on purpose:
# weak models follow short, literal instructions far better than elegant ones.
_PROMPTED_TOOLS = """You can call tools. To call one, reply with ONLY this JSON:
{{"tool": "<name>", "args": {{...}}}}
When you have enough information, reply with ONLY this JSON:
{{"final": "<your answer>"}}
Never write anything outside the JSON object.

Available tools:
{tools}"""


class BudgetExceeded(RuntimeError):
    """The task hit a hard ceiling. Not a provider failure — do not fail over."""


class _Guard:
    """Enforces the budget across a strategy's whole execution."""

    def __init__(self, budget: Budget):
        self.budget = budget
        self.started = time.monotonic()
        self.tool_calls = 0

    def check_clock(self) -> None:
        if time.monotonic() - self.started > self.budget.wall_clock_s:
            raise BudgetExceeded(f"wall clock exceeded {self.budget.wall_clock_s}s")

    def count_tool_call(self) -> None:
        self.tool_calls += 1
        if self.tool_calls > self.budget.max_tool_calls:
            raise BudgetExceeded(f"tool-call budget exceeded ({self.budget.max_tool_calls})")


def _user_message(inputs: dict) -> str:
    return str(inputs.get("prompt", "") or "")


def _system(inputs: dict) -> str:
    return str(inputs.get("system", "") or "")


def _max_tokens(backend) -> int:
    """Never ask for more output than the model can produce — some providers 400 on it
    rather than clamping."""
    return min(4096, getattr(backend.card, "max_output_tokens", 4096) or 4096)


# --- simple, no tools ---------------------------------------------------------------

def plain(task, backend, toolbox, inputs, budget) -> str:
    return backend.chat(ChatRequest(
        system=_system(inputs),
        messages=[Message("user", _user_message(inputs))],
        max_tokens=_max_tokens(backend),
        temperature=inputs.get("temperature"),
    )).text


def json_native(task, backend, toolbox, inputs, budget) -> str:
    """Ask for JSON and validate it. The provider may or may not enforce the format, so
    the tolerant parser runs either way and one repair turn is allowed."""
    system = _system(inputs) + "\nRespond with ONLY a JSON object."
    messages = [Message("user", _user_message(inputs))]
    result = backend.chat(ChatRequest(system=system, messages=messages))

    if jsonx.loads_object(result.text) is not None:
        return result.text

    # One repair turn, quoting what was wrong. Cheaper than failing over.
    messages += [
        Message("assistant", result.text),
        Message("user", "That was not valid JSON. Reply with ONLY a valid JSON object."),
    ]
    return backend.chat(ChatRequest(system=system, messages=messages)).text


prompted_json = json_native      # same shape; the router distinguishes them for ranking


# --- tools ---------------------------------------------------------------------------

def action_call(task, backend, toolbox, inputs, budget) -> str:
    """One forced tool call, executed. The call IS the outcome, so the model never has
    to read a result — which is why a model that cannot loop can still do this."""
    guard = _Guard(budget)
    specs = toolbox.specs({t.name for t in task.tools} or None)
    result = backend.chat(ChatRequest(
        system=_system(inputs),
        messages=[Message("user", _user_message(inputs))],
        tools=specs,
        tool_choice="required" if len(specs) > 1 else (specs[0].name if specs else "auto"),
    ))
    if not result.tool_calls:
        return result.text or ""
    guard.count_tool_call()
    return toolbox.execute(result.tool_calls[0]).content


def native_loop(task, backend, toolbox, inputs, budget) -> str:
    """The ordinary agentic loop: call tools until the model answers.

    Terminates three ways — the model stops calling tools, the step budget runs out, or
    the guard trips. An unbounded loop against a paid model is a bill, not a feature.
    """
    guard = _Guard(budget)
    specs = toolbox.specs({t.name for t in task.tools} or None)
    system = _system(inputs)
    messages: list[Message] = [Message("user", _user_message(inputs))]

    for _ in range(max(1, task.max_tool_steps)):
        guard.check_clock()
        result = backend.chat(ChatRequest(system=system, messages=messages, tools=specs))
        if not result.tool_calls:
            return result.text

        messages.append(Message("assistant", result.text, tool_calls=result.tool_calls))
        results = []
        for call in result.tool_calls:
            guard.count_tool_call()
            results.append(toolbox.execute(call))
        messages.append(Message("tool", tool_results=tuple(results)))

    # Out of steps with tools still being requested: ask for an answer from what it has,
    # rather than returning nothing.
    messages.append(Message("user", "Step budget reached. Answer now from what you have."))
    return backend.chat(ChatRequest(system=system, messages=messages)).text


def restricted_loop(task, backend, toolbox, inputs, budget) -> str:
    """A loop for models that can just barely manage one.

    Three restrictions, each targeting a way small models fail: the tool set is trimmed
    so there is less to get wrong; the first call is forced so the model cannot wander;
    and after each result Python injects a plain-language state summary, so the model
    never has to remember anything across turns.
    """
    guard = _Guard(budget)
    keep = set(task.required_tools) or {t.name for t in task.tools}
    specs = toolbox.specs(keep)[:3]
    system = _system(inputs)
    messages: list[Message] = [Message("user", _user_message(inputs))]
    gathered: list[str] = []

    for step in range(2):
        guard.check_clock()
        result = backend.chat(ChatRequest(
            system=system, messages=messages, tools=specs,
            # Force a call on the first step only; afterwards it must be free to answer.
            tool_choice="required" if step == 0 else "auto",
        ))
        if not result.tool_calls:
            return result.text

        messages.append(Message("assistant", result.text, tool_calls=result.tool_calls))
        results = []
        for call in result.tool_calls:
            guard.count_tool_call()
            res = toolbox.execute(call)
            results.append(res)
            gathered.append(f"{call.name}: {res.content}")
        messages.append(Message("tool", tool_results=tuple(results)))
        # The state summary: everything known so far, restated as plain text.
        messages.append(Message("user", "So far you have learned:\n" + "\n".join(gathered)
                                + "\n\nAnswer the original question now."))

    return backend.chat(ChatRequest(system=system, messages=messages)).text


def prefetch_single_shot(task, backend, toolbox, inputs, budget) -> str:
    """Python runs the plan; the model writes the answer.

    The highest-value degradation and the lowest-risk one, because the model is asked to
    do only the thing every tier can do. `task.prefetch` performs the retrieval
    deterministically, so there is no control flow to get wrong and exactly one call.
    """
    guard = _Guard(budget)
    guard.check_clock()
    context = task.prefetch(inputs=inputs, toolbox=toolbox) if task.prefetch else ""
    system = _system(inputs)
    user = (f"{_user_message(inputs)}\n\n"
            f"Use only the information below to answer.\n\n{context}")
    return backend.chat(ChatRequest(system=system,
                                    messages=[Message("user", user)])).text


def prompted_tool_json(task, backend, toolbox, inputs, budget) -> str:
    """Tool emulation for backends whose native support is unproven.

    Capped at two steps: this path exists for models we are least sure of, and a
    prompt-parsed protocol degrades fast. A reply that is neither a tool call nor a
    final answer is returned as-is rather than retried forever.
    """
    guard = _Guard(budget)
    specs = toolbox.specs({t.name for t in task.tools} or None)
    catalogue = "\n".join(f"- {s.name}: {s.description} args={list(s.parameters.get('properties', {}))}"
                          for s in specs)
    system = (_system(inputs) + "\n\n" + _PROMPTED_TOOLS.format(tools=catalogue))
    messages: list[Message] = [Message("user", _user_message(inputs))]

    for _ in range(2):
        guard.check_clock()
        text = backend.chat(ChatRequest(system=system, messages=messages)).text
        parsed = jsonx.loads_object(text)
        if parsed is None:
            return text                       # not protocol-shaped; take it at face value
        if "final" in parsed:
            return str(parsed["final"])
        name = parsed.get("tool")
        if not name:
            return text

        guard.count_tool_call()
        res = toolbox.execute(ToolCall(id=f"p{guard.tool_calls}", name=str(name),
                                       args=parsed.get("args") or {}))
        messages += [
            Message("assistant", text),
            Message("user", f"Result of {name}:\n{res.content}\n\n"
                            f"Now reply with ONLY {{\"final\": \"<answer>\"}}."),
        ]
    return ""


def deterministic_fallback(task, backend, toolbox, inputs, budget) -> str:
    """No model at all. The last resort that keeps a run alive."""
    return str(task.fallback(inputs) if task.fallback else "")


EXECUTORS = {
    Strategy.PLAIN: plain,
    Strategy.JSON_NATIVE: json_native,
    Strategy.PROMPTED_JSON: prompted_json,
    Strategy.NATIVE_LOOP: native_loop,
    Strategy.RESTRICTED_LOOP: restricted_loop,
    Strategy.ACTION_CALL: action_call,
    Strategy.PREFETCH_SINGLE_SHOT: prefetch_single_shot,
    Strategy.PROMPTED_TOOL_JSON: prompted_tool_json,
    Strategy.DETERMINISTIC_FALLBACK: deterministic_fallback,
}
