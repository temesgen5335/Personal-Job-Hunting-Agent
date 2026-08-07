"""The seam between the harness and whatever the host application can do.

A `ToolBox` owns the tools; the harness only asks for their specs and asks it to run a
call. That is what keeps agentkit domain-agnostic — it never learns what a tool does,
only that one exists and what shape its arguments take.

Two properties matter more than the plumbing:

- **A tool that raises must not kill the loop.** The failure becomes a `ToolResult` with
  `is_error=True`, so the model can see what went wrong and try something else. An
  exception would throw away the whole conversation.
- **A name the model invented must be refused, not resolved.** Unknown names return an
  error result and are counted; a model that has been steered toward a tool it may not
  have must not be able to spin the loop trying.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from agentkit.llm.types import ToolCall, ToolResult, ToolSpec, validate_tool_schema

# Truncated so one chatty tool cannot eat the whole context window.
MAX_RESULT_CHARS = 4000


@dataclass(frozen=True)
class Tool:
    spec: ToolSpec
    run: Callable[[dict], str]

    @property
    def name(self) -> str:
        return self.spec.name


@dataclass
class ToolBox:
    """A registry the harness can call. Validates schemas at registration, because an
    unportable schema is a build-time mistake, not a runtime mystery."""

    tools: dict[str, Tool] = field(default_factory=dict)
    max_result_chars: int = MAX_RESULT_CHARS
    calls: int = 0
    unknown_calls: int = 0

    def register(self, spec: ToolSpec, run: Callable[[dict], str]) -> None:
        problems = validate_tool_schema(spec)
        if problems:
            raise ValueError(f"tool {spec.name!r} has an unportable schema: {problems}")
        if spec.name in self.tools:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self.tools[spec.name] = Tool(spec, run)

    def specs(self, only: set[str] | None = None) -> tuple[ToolSpec, ...]:
        return tuple(t.spec for name, t in self.tools.items()
                     if only is None or name in only)

    def execute(self, call: ToolCall) -> ToolResult:
        """Run one call. Never raises."""
        self.calls += 1

        if call.parse_error:
            # Tell the model precisely what was wrong with its arguments; that is
            # usually enough for it to correct on the next turn.
            return ToolResult(call.id, call.name,
                              f"Your arguments could not be parsed: {call.parse_error}. "
                              f"Send valid JSON matching the tool's schema.",
                              is_error=True)

        tool = self.tools.get(call.name)
        if tool is None:
            self.unknown_calls += 1
            available = ", ".join(sorted(self.tools)) or "(none)"
            return ToolResult(call.id, call.name,
                              f"No tool named {call.name!r}. Available: {available}.",
                              is_error=True)

        try:
            output = tool.run(call.args)
        except Exception as exc:  # noqa: BLE001 — a broken tool must not end the run
            return ToolResult(call.id, call.name,
                              f"{type(exc).__name__}: {exc}", is_error=True)

        text = output if isinstance(output, str) else str(output)
        if len(text) > self.max_result_chars:
            text = text[:self.max_result_chars] + f"\n…[truncated at {self.max_result_chars} chars]"
        return ToolResult(call.id, call.name, text)
