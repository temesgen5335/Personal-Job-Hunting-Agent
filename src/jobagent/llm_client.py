"""The pipeline's LLM — a thin adapter over agentkit's reusable `LLMService`.

There used to be a second multi-provider router here (its own registry, failover, usage
ledger and free-model fan-out) parallel to `agentkit.llm`. That was redundant: the chain,
ordered failover, circuit breaker, capability routing, provider registry and OpenRouter
free-model fan-out all live in `agentkit.llm` now, and the assistant already used them.

This module exists only so pipeline code keeps calling `build_llm(settings)` and gets an
object exposing `.complete(system, user, json_mode=False) -> str`, `.chain`, and `.ledger`.
`jobagent.config.Settings` already carries every attribute agentkit's `from_settings`
reads (duck-typed), so no adapter shim is needed.
"""

from __future__ import annotations

from agentkit.llm.service import AllProvidersFailed, LLMService

__all__ = ["build_llm", "LLMService", "AllProvidersFailed"]


def build_llm(settings, temperature: float = 0.3) -> LLMService | None:
    """Build the pipeline's LLM from settings, or None when no provider is usable.

    Returns None on an empty chain so callers can report "no LLM configured" exactly as
    before. `temperature` defaults to 0.3 — the deterministic value scoring and generation
    have always used — threaded into every completion.
    """
    service = LLMService.from_settings(settings, temperature=temperature)
    return service if service.backends else None
