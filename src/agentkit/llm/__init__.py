"""Capability-aware multi-provider LLM access: chat with tools, model tiering,
classified failover."""

from agentkit.llm.capabilities import ModelCard, Tier, resolve_card
from agentkit.llm.errors import Classified, Verdict, classify
from agentkit.llm.health import Breaker
from agentkit.llm.router import choose_strategy, describe, plans_for
from agentkit.llm.tasks import (
    Budget,
    NoCapableModel,
    Plan,
    Rejection,
    Strategy,
    TaskOutcome,
    TaskSpec,
)
from agentkit.llm.types import (
    ChatRequest,
    ChatResult,
    Message,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
    assert_wellformed,
    validate_tool_schema,
)

__all__ = [
    "ChatRequest", "ChatResult", "Message", "ToolCall", "ToolResult", "ToolSpec",
    "Usage", "assert_wellformed", "validate_tool_schema",
    "ModelCard", "Tier", "resolve_card",
    "Classified", "Verdict", "classify",
    "Breaker", "choose_strategy", "describe", "plans_for",
    "Budget", "NoCapableModel", "Plan", "Rejection", "Strategy", "TaskOutcome", "TaskSpec",
]
