"""Anthropic chat backend.

Anthropic differs from the OpenAI family in three ways that all have to be absorbed
here rather than leaking into the IR:

- `system` is a top-level parameter, not a message.
- Tool calls arrive as `tool_use` blocks inside assistant content, not a separate field.
- Every `tool_result` for a turn must sit in ONE user message directly after the
  assistant turn that requested them. This is exactly why the IR groups results into a
  single `role="tool"` message: the translation is a fold, not a search.

`max_tokens` is required by the API, so it is always sent.
"""

from __future__ import annotations

from agentkit.llm.capabilities import ModelCard, resolve_card
from agentkit.llm.types import ChatRequest, ChatResult, ToolCall, Usage

_STOP_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "refusal": "content_filter",
}


def to_anthropic_messages(req: ChatRequest) -> list[dict]:
    out: list[dict] = []
    for m in req.messages:
        if m.role == "tool":
            # One user message carrying every result of the preceding turn.
            out.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": r.call_id,
                 "content": r.content, **({"is_error": True} if r.is_error else {})}
                for r in m.tool_results
            ]})
        elif m.role == "assistant" and m.tool_calls:
            blocks: list[dict] = []
            if m.text:
                blocks.append({"type": "text", "text": m.text})
            blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.args}
                       for c in m.tool_calls]
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": m.role, "content": m.text})
    return out


def to_anthropic_tools(req: ChatRequest) -> tuple[list[dict] | None, dict | None]:
    if not req.tools:
        return None, None
    tools = [{"name": t.name, "description": t.description, "input_schema": t.parameters}
             for t in req.tools]
    if req.tool_choice == "auto":
        choice = {"type": "auto"}
    elif req.tool_choice == "required":
        choice = {"type": "any"}
    elif req.tool_choice == "none":
        # Anthropic has no "none"; withholding the tools is the equivalent.
        return None, None
    else:
        choice = {"type": "tool", "name": req.tool_choice}
    return tools, choice


class AnthropicChat:
    def __init__(self, name: str, api_key: str, model: str, card: ModelCard | None = None):
        self.name = name
        self.model = model
        self._api_key = api_key
        self.card = card or resolve_card(name, model)
        self._client = None

    def _ensure(self):
        if self._client is None:
            import anthropic  # lazy
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def chat(self, req: ChatRequest) -> ChatResult:
        tools, choice = to_anthropic_tools(req)
        kwargs: dict = {
            "model": self.model,
            "messages": to_anthropic_messages(req),
            "max_tokens": req.max_tokens,          # required by this API
            "timeout": req.timeout_s,
        }
        if req.system:
            kwargs["system"] = req.system
        if req.temperature is not None:
            kwargs["temperature"] = req.temperature
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = choice

        resp = self._ensure().messages.create(**kwargs)

        text_parts, calls = [], []
        for block in resp.content:
            kind = getattr(block, "type", None)
            if kind == "text":
                text_parts.append(block.text)
            elif kind == "tool_use":
                # Anthropic hands back a parsed object, so there is no arguments
                # string to repair — parse_error is structurally impossible here.
                calls.append(ToolCall(id=block.id, name=block.name,
                                      args=dict(block.input or {})))

        usage = None
        if getattr(resp, "usage", None):
            usage = Usage(input_tokens=getattr(resp.usage, "input_tokens", 0) or 0,
                          output_tokens=getattr(resp.usage, "output_tokens", 0) or 0)

        return ChatResult(
            text="".join(text_parts),
            tool_calls=tuple(calls),
            stop_reason=_STOP_REASONS.get(getattr(resp, "stop_reason", "") or "", "other"),
            provider=self.name, model=self.model, usage=usage, raw=resp,
        )
