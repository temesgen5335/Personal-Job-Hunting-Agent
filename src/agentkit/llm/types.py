"""Provider-neutral chat IR.

`MultiLLM.complete(system, user)` cannot express an agent loop — no message history,
no tool calls, no tool results. This is the shape that can, normalized across the two
provider families the project already depends on.

Two normalization decisions carry their weight and are worth stating:

1. **`system` is a request field, not a message.** Anthropic takes a top-level
   `system=`; OpenAI takes a message. Making it a field means the adapter never has to
   guess, and "two system messages" — which Anthropic cannot express — is
   unrepresentable rather than silently mangled.

2. **One `role="tool"` message carries ALL results of a turn.** Anthropic requires
   every `tool_result` for a turn to sit in a single user message directly after the
   assistant's `tool_use` turn; OpenAI wants one message per call. Grouping them in the
   IR makes both translations a local fold/expand — lossless in both directions. Storing
   one result per message would force the Anthropic adapter to scan forward and
   re-group, which breaks as soon as history is truncated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["user", "assistant", "tool"]
StopReason = Literal["stop", "tool_calls", "length", "content_filter", "other"]


@dataclass(frozen=True)
class ToolCall:
    """A model's request to run one tool."""

    id: str
    name: str
    args: dict = field(default_factory=dict)   # ALWAYS a parsed dict, never a string
    raw_args: str = ""                          # OpenAI's JSON string, kept for debugging
    parse_error: str = ""                       # non-empty → the model emitted bad args

    @property
    def ok(self) -> bool:
        return not self.parse_error


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class Message:
    role: Role
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()      # assistant turns only
    tool_results: tuple[ToolResult, ...] = ()  # role="tool" only; ALL results of one turn


@dataclass(frozen=True)
class ToolSpec:
    """A tool offered to the model. `parameters` is JSON Schema, restricted — see
    `validate_tool_schema`."""

    name: str
    description: str
    parameters: dict = field(default_factory=lambda: {"type": "object", "properties": {}})


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class ChatRequest:
    messages: list[Message]
    system: str = ""
    tools: tuple[ToolSpec, ...] = ()
    # "auto" = model decides; "required" = must call something; "none" = must not;
    # any other string = force that specific tool by name.
    tool_choice: str = "auto"
    max_tokens: int = 4096
    temperature: float | None = None
    timeout_s: float = 60.0


@dataclass(frozen=True)
class ChatResult:
    text: str
    tool_calls: tuple[ToolCall, ...]
    stop_reason: StopReason
    provider: str
    model: str
    usage: Usage | None = None
    raw: Any = None            # provider response; never read by callers


# --- tool schema validation --------------------------------------------------------
# Deliberately a small subset. Gemini's OpenAI-compat endpoint and grammar-constrained
# local servers (vLLM/Ollama) reject or silently mangle the rest, and weak models fail
# hardest on nesting — so a schema that cannot be expressed everywhere is a bug at
# registration time, not a mystery at run time.

_PRIMITIVES = {"string", "number", "integer", "boolean"}


def validate_tool_schema(spec: ToolSpec, *, _depth: int = 0) -> list[str]:
    """Return a list of problems; empty means the schema is portable."""
    problems: list[str] = []
    schema = spec.parameters

    if not isinstance(schema, dict) or schema.get("type") != "object":
        return [f"{spec.name}: parameters must be a JSON Schema object"]

    for banned in ("$ref", "anyOf", "oneOf", "allOf", "not"):
        if banned in schema:
            problems.append(f"{spec.name}: '{banned}' is not portable across providers")

    props = schema.get("properties", {})
    if not isinstance(props, dict):
        return [f"{spec.name}: 'properties' must be an object"]

    for pname, p in props.items():
        if not isinstance(p, dict):
            problems.append(f"{spec.name}.{pname}: property must be a schema object")
            continue
        ptype = p.get("type")
        if not p.get("description"):
            problems.append(f"{spec.name}.{pname}: needs a description (the model reads it)")
        if ptype in _PRIMITIVES or "enum" in p:
            continue
        if ptype == "array":
            items = p.get("items", {})
            if items.get("type") not in _PRIMITIVES and "enum" not in items:
                problems.append(f"{spec.name}.{pname}: arrays must hold primitives")
            continue
        if ptype == "object":
            if _depth >= 1:
                problems.append(f"{spec.name}.{pname}: nesting deeper than one level")
            else:
                problems += validate_tool_schema(
                    ToolSpec(f"{spec.name}.{pname}", "", p), _depth=_depth + 1)
            continue
        problems.append(f"{spec.name}.{pname}: unsupported type {ptype!r}")

    for req in schema.get("required", []):
        if req not in props:
            problems.append(f"{spec.name}: required field {req!r} is not in properties")
    return problems


def assert_wellformed(messages: list[Message]) -> None:
    """Every tool result must answer a call from the immediately preceding turn.

    Anthropic rejects a `tool_result` whose `tool_use_id` is not in the previous turn,
    and the 400 it returns looks like a model failure rather than a history bug. History
    truncation must therefore drop call/result pairs atomically — this catches it if it
    does not.
    """
    for i, m in enumerate(messages):
        if m.role != "tool":
            continue
        if i == 0 or messages[i - 1].role != "assistant":
            raise ValueError("tool results must directly follow an assistant turn")
        offered = {c.id for c in messages[i - 1].tool_calls}
        for r in m.tool_results:
            if r.call_id not in offered:
                raise ValueError(
                    f"tool result {r.call_id!r} answers no call in the preceding turn"
                )
