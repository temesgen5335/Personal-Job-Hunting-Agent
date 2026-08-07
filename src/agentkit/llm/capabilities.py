"""What a model can actually do — and how confidently we know it.

The registry below is seeded from a measured spike (Aug 2026, n=5-6 against real
prompts), not from vendor marketing. The finding that shaped the design:
`llama-3.1-8b-instant` emits well-formed tool calls 5/5 and picks the right tool 5/5,
then fails 5/5 to use the tool *result* to answer. It can call a tool; it cannot run a
loop.

That is why capability booleans are TRI-STATE and why a probe may never set `tier`:

    native_tools=True   proven to emit usable tool calls
    native_tools=False  proven not to
    native_tools=None   unproven — pick a strategy that works either way

A cheap probe can observe "does it accept the tools parameter and emit a call". It
cannot observe "can it carry state across a tool result", which is the thing that
actually decides whether an agent loop works. Tier encodes that judgement and only a
human or a real eval sets it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import IntEnum


class Tier(IntEnum):
    """Ordered so admission checks are plain comparisons. UNKNOWN sorts below
    everything and is a real value, never a guess at a real tier."""

    UNKNOWN = -1
    TINY = 0        # <=4B or heavily quantized
    WEAK = 1        # 7-9B — can answer, cannot orchestrate
    STANDARD = 2    # ~70B / flagship-mini — reliable single-loop tool use
    STRONG = 3      # frontier — multi-step planning


@dataclass(frozen=True)
class ModelCard:
    model: str
    tier: Tier = Tier.UNKNOWN
    context_tokens: int = 0            # 0 = unknown
    max_output_tokens: int = 4096
    native_tools: bool | None = None   # tri-state; see module docstring
    tool_loop: bool | None = None      # can it USE a tool result? the loop-critical one
    json_object: bool | None = None
    source: str = "default"            # settings | registry | probe | default
    notes: str = ""


def _normalize(model: str) -> str:
    """Fold the many spellings of one model into a registry key.

    `meta-llama/llama-3.3-70b-instruct:free` and `llama-3.3-70b-versatile` are the same
    family reached through different providers, and OpenRouter suffixes/vendor prefixes
    would otherwise each need their own entry.
    """
    m = model.strip().lower()
    m = re.sub(r":(free|nitro|beta|extended)$", "", m)
    m = m.split("/")[-1]                      # strip vendor prefix
    m = re.sub(r"-\d{8}$", "", m)             # strip trailing date stamp
    return re.sub(r"-latest$", "", m)


# Measured, not assumed. See .claude/memory.md "Free-model capability spike".
_EXACT: dict[str, ModelCard] = {
    "llama-3.1-8b-instant": ModelCard(
        "llama-3.1-8b-instant", Tier.WEAK, 131072, 8192,
        native_tools=True, tool_loop=False, json_object=True, source="registry",
        notes="MEASURED: emits tool calls 5/5, selects correctly 5/5, uses tool RESULT 0/5"),
    "llama-3.3-70b-versatile": ModelCard(
        "llama-3.3-70b-versatile", Tier.STANDARD, 131072, 32768,
        native_tools=True, tool_loop=True, json_object=True, source="registry",
        notes="MEASURED 5/5 loop; over-calls tools on conversational closings"),
    "llama-3.3-70b-instruct": ModelCard(
        "llama-3.3-70b-instruct", Tier.STANDARD, 131072, 8192,
        native_tools=True, tool_loop=True, json_object=True, source="registry"),
    "gpt-oss-120b": ModelCard(
        "gpt-oss-120b", Tier.STANDARD, 131072, 32768,
        native_tools=True, tool_loop=True, json_object=True, source="registry",
        notes="MEASURED 20/20 across emit/loop/select/restraint; ~3x slower than 70b"),
    "gpt-oss-20b": ModelCard("gpt-oss-20b", Tier.WEAK, 131072, 8192,
                             native_tools=True, json_object=True, source="registry"),
    "gemini-2.0-flash": ModelCard(
        "gemini-2.0-flash", Tier.STANDARD, 1_048_576, 8192,
        native_tools=None, json_object=True, source="registry",
        notes="tools UNPROVEN through the OpenAI-compat endpoint — spike before trusting"),
    "gpt-4o-mini": ModelCard("gpt-4o-mini", Tier.STANDARD, 128000, 16384,
                             native_tools=True, tool_loop=True, json_object=True,
                             source="registry"),
    "claude-sonnet-4-6": ModelCard("claude-sonnet-4-6", Tier.STRONG, 200000, 8192,
                                   native_tools=True, tool_loop=True, source="registry"),
}

_PATTERNS: tuple[tuple[re.Pattern, ModelCard], ...] = (
    (re.compile(r"^gpt-4o"), ModelCard("gpt-4o", Tier.STRONG, 128000, 16384,
                                       native_tools=True, tool_loop=True, json_object=True,
                                       source="registry")),
    (re.compile(r"^claude-.*opus"), ModelCard("claude-opus", Tier.STRONG, 200000, 8192,
                                              native_tools=True, tool_loop=True,
                                              source="registry")),
    (re.compile(r"^claude-.*haiku"), ModelCard("claude-haiku", Tier.STANDARD, 200000, 8192,
                                               native_tools=True, tool_loop=True,
                                               source="registry")),
    (re.compile(r"^gemini-.*pro"), ModelCard("gemini-pro", Tier.STRONG, 1_048_576, 8192,
                                             native_tools=None, source="registry")),
    (re.compile(r"^llama-3\.1-70b"), ModelCard("llama-3.1-70b", Tier.STANDARD, 131072, 8192,
                                               native_tools=True, source="registry")),
)

# Tier is a property of (provider, model), not model alone: the same weights reached
# through a router can behave differently per request.
_PROVIDER_OVERLAYS: dict[str, dict] = {
    "openrouter": {
        "native_tools": None,
        "notes": "OpenRouter :free routes to varying upstreams — capability differs per request",
    },
}

_TIER_NAMES = {t.name.lower(): t for t in Tier}


def parse_tier_overrides(raw: str) -> dict[str, Tier]:
    """`"custom=standard,groq:llama-3.3-70b-versatile=strong"` → lookup table.

    Keys are matched most-specific first: `provider:model`, then bare `provider`.
    Unparseable entries are skipped rather than raising — a typo in config should not
    take the whole agent down.
    """
    out: dict[str, Tier] = {}
    for part in (raw or "").split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        tier = _TIER_NAMES.get(value.strip().lower())
        if tier is not None and key.strip():
            out[key.strip().lower()] = tier
    return out


def resolve_card(provider: str, model: str, settings=None) -> ModelCard:
    """Settings override → static registry → provider overlay → UNKNOWN.

    Every settings read uses getattr with a default: the LLM test fixtures pass a
    SimpleNamespace with a handful of attributes, and this must not require more.
    """
    key = _normalize(model)
    card = _EXACT.get(key)
    if card is None:
        for pattern, candidate in _PATTERNS:
            if pattern.match(key):
                card = candidate
                break
    if card is None:
        card = ModelCard(model=model, tier=Tier.UNKNOWN, source="default",
                         notes="unrecognized model — set LLM_TIER_OVERRIDES to declare its tier")
    card = replace(card, model=model)

    overlay = _PROVIDER_OVERLAYS.get(provider.lower())
    if overlay:
        card = replace(card, **overlay)

    overrides = parse_tier_overrides(getattr(settings, "llm_tier_overrides", "") if settings else "")
    for candidate in (f"{provider.lower()}:{model.lower()}", provider.lower()):
        if candidate in overrides:
            card = replace(card, tier=overrides[candidate], source="settings")
            break
    return card
