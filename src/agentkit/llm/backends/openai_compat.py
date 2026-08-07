"""OpenAI-compatible chat backend: OpenAI, Groq, OpenRouter, Gemini, Ollama, vLLM.

Standalone rather than subclassing any host-application backend — agentkit must not
import the application it serves. The overlap with a plain completion client is a
handful of lines and they diverge immediately, since only this one speaks tools.

The SDK is imported lazily so this module loads with nothing installed, matching the
discipline the rest of the project already uses.
"""

from __future__ import annotations

import json

from agentkit.llm.capabilities import ModelCard, resolve_card
from agentkit.llm.types import (
    ChatRequest,
    ChatResult,
    Message,
    ToolCall,
    Usage,
)

_STOP_REASONS = {
    "stop": "stop",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "length": "length",
    "content_filter": "content_filter",
}


def _parse_args(raw: str) -> tuple[dict, str]:
    """OpenAI sends tool arguments as a JSON *string*, occasionally malformed or empty.

    Never raise here: a bad-args tool call is information the loop can act on (it can
    tell the model what broke), whereas an exception loses the turn entirely. But do not
    cry wolf either — a false parse_error costs a whole turn telling the model to fix
    something that was fine.

    Three spellings all mean "no arguments" and are accepted: `""`, `"{}"`, and the
    literal `"null"` — which is what Groq actually returns for a no-parameter tool
    (observed live, and something no fake would have reproduced). A JSON array, string
    or number IS genuinely wrong and is still reported.
    """
    text = (raw or "").strip()
    if not text:
        return {}, ""
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError) as exc:
        return {}, f"invalid JSON arguments: {exc}"
    if parsed is None:
        return {}, ""
    if not isinstance(parsed, dict):
        return {}, f"arguments must be an object, got {type(parsed).__name__}"
    return parsed, ""


def to_openai_messages(req: ChatRequest) -> list[dict]:
    """IR → OpenAI wire format.

    The one expansion: our single `role="tool"` message holding every result of a turn
    becomes one OpenAI message per result.
    """
    out: list[dict] = []
    if req.system:
        out.append({"role": "system", "content": req.system})
    for m in req.messages:
        if m.role == "tool":
            for r in m.tool_results:
                out.append({"role": "tool", "tool_call_id": r.call_id, "content": r.content})
        elif m.role == "assistant" and m.tool_calls:
            out.append({
                "role": "assistant",
                "content": m.text or None,
                "tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.name,
                                  "arguments": c.raw_args or json.dumps(c.args)}}
                    for c in m.tool_calls
                ],
            })
        else:
            out.append({"role": m.role, "content": m.text})
    return out


def to_openai_tools(req: ChatRequest) -> tuple[list[dict] | None, object]:
    if not req.tools:
        return None, None
    tools = [{"type": "function",
              "function": {"name": t.name, "description": t.description,
                           "parameters": t.parameters}}
             for t in req.tools]
    choice: object
    if req.tool_choice in ("auto", "required", "none"):
        choice = req.tool_choice
    else:
        choice = {"type": "function", "function": {"name": req.tool_choice}}
    return tools, choice


class OpenAICompatChat:
    """`.name`/`.model`/`.card` plus `.chat()` — the protocol the router expects."""

    def __init__(self, name: str, api_key: str, model: str,
                 base_url: str | None = None, card: ModelCard | None = None):
        self.name = name
        self.model = model
        self._api_key = api_key or "not-needed"    # local servers often want a placeholder
        self._base_url = base_url
        self.card = card or resolve_card(name, model)
        self._client = None

    def _ensure(self):
        if self._client is None:
            from openai import OpenAI  # lazy
            self._client = OpenAI(api_key=self._api_key, base_url=self._base_url)
        return self._client

    def chat(self, req: ChatRequest) -> ChatResult:
        tools, choice = to_openai_tools(req)
        kwargs: dict = {
            "model": self.model,
            "messages": to_openai_messages(req),
            "max_tokens": req.max_tokens,
            "timeout": req.timeout_s,
        }
        if req.temperature is not None:
            kwargs["temperature"] = req.temperature
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = choice

        resp = self._ensure().chat.completions.create(**kwargs)
        choice0 = resp.choices[0]
        msg = choice0.message

        calls = []
        for c in (getattr(msg, "tool_calls", None) or []):
            args, err = _parse_args(c.function.arguments)
            calls.append(ToolCall(id=c.id, name=c.function.name, args=args,
                                  raw_args=c.function.arguments or "", parse_error=err))

        usage = None
        if getattr(resp, "usage", None):
            usage = Usage(input_tokens=getattr(resp.usage, "prompt_tokens", 0) or 0,
                          output_tokens=getattr(resp.usage, "completion_tokens", 0) or 0)

        return ChatResult(
            # content is None on a pure tool turn — normalize so callers never see None.
            text=msg.content or "",
            tool_calls=tuple(calls),
            stop_reason=_STOP_REASONS.get(choice0.finish_reason or "", "other"),
            provider=self.name, model=self.model, usage=usage, raw=resp,
        )


def assistant_turn(result: ChatResult) -> Message:
    """The assistant turn to append to history after a ChatResult — so callers do not
    hand-build the message shape that `assert_wellformed` then checks."""
    return Message(role="assistant", text=result.text, tool_calls=result.tool_calls)
