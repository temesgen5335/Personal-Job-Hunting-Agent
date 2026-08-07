"""What a model can do — and how confidently we know it.

Resolution is deliberately layered so an unrecognized model still lands somewhere
sensible instead of UNKNOWN. Adding a provider key should just work; a registry that
only knows the exact strings someone happened to test is a registry that degrades every
new model to the slow path.

    settings override  → the user knows what they installed
    exact entry        → measured, wins over everything derived
    family pattern     → gpt-4o / claude-sonnet / gemini-flash / qwen-max …
    parameter size     → "…-72b-instruct" ⇒ STANDARD; covers open models generically
    UNKNOWN            → treated as WEAK at routing time, never assumed capable

`tool_loop` is DERIVED, not guessed per model: emitting a tool call and carrying state
across a tool *result* are different abilities, and they come apart. Measured on
llama-3.1-8b — emits 5/5, selects 5/5, uses the result 0/5. The derivation
(`native_tools and tier >= STANDARD`) reproduces that measurement, which is the reason
to trust it for models nobody has measured yet. An explicit measurement always wins.
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
    WEAK = 1        # 5-13B — can answer, cannot orchestrate
    STANDARD = 2    # ~14B-100B / flagship-mini — reliable single-loop tool use
    STRONG = 3      # frontier — multi-step planning


@dataclass(frozen=True)
class ModelCard:
    model: str
    tier: Tier = Tier.UNKNOWN
    context_tokens: int = 0            # 0 = unknown
    max_output_tokens: int = 4096
    native_tools: bool | None = None   # None = unproven, False = proven absent
    json_object: bool | None = None
    # Set ONLY from a real measurement; otherwise `tool_loop` derives it.
    tool_loop_measured: bool | None = None
    source: str = "default"            # settings | measured | family | size | default
    notes: str = ""

    @property
    def tool_loop(self) -> bool | None:
        """Can this model use a tool RESULT, not merely emit a call?

        Derived unless measured. A model that cannot emit calls certainly cannot loop;
        one whose tool support is unproven has an unproven loop; and below STANDARD the
        loop is where small models fall over — which is what the 8B measurement showed.
        """
        if self.tool_loop_measured is not None:
            return self.tool_loop_measured
        if self.native_tools is None:
            return None
        if not self.native_tools:
            return False
        return self.tier >= Tier.STANDARD


def _normalize(model: str) -> str:
    """Fold the many spellings of one model into a registry key."""
    m = model.strip().lower()
    m = re.sub(r":(free|nitro|beta|extended|thinking)$", "", m)
    m = m.split("/")[-1]                      # strip vendor prefix
    m = re.sub(r"-\d{8}$", "", m)             # strip trailing date stamp
    m = re.sub(r"@\d+$", "", m)               # strip Ollama tag
    return re.sub(r"-latest$", "", m)


# --- measured entries: these override anything derived -----------------------------
# See .claude/memory.md "Free-model capability spike".
_MEASURED: dict[str, ModelCard] = {
    "llama-3.1-8b-instant": ModelCard(
        "llama-3.1-8b-instant", Tier.WEAK, 131072, 8192,
        native_tools=True, json_object=True, tool_loop_measured=False, source="measured",
        notes="MEASURED: emits 5/5, selects 5/5, uses tool result 0/5"),
    "llama-3.3-70b-versatile": ModelCard(
        "llama-3.3-70b-versatile", Tier.STANDARD, 131072, 32768,
        native_tools=True, json_object=True, tool_loop_measured=True, source="measured",
        notes="MEASURED 5/5 loop; over-calls tools on conversational closings"),
    "gpt-oss-120b": ModelCard(
        "gpt-oss-120b", Tier.STANDARD, 131072, 32768,
        native_tools=True, json_object=True, tool_loop_measured=True, source="measured",
        notes="MEASURED 20/20 emit/loop/select/restraint; ~3x slower than llama-3.3-70b"),
}


def _f(tier, ctx, out, *, tools=True, json_obj=True, notes=""):
    """Family-pattern card factory — model name is filled in by the resolver."""
    return ModelCard("", tier, ctx, out, native_tools=tools, json_object=json_obj,
                     source="family", notes=notes)


# Ordered: first match wins, so put the more specific pattern first.
_FAMILIES: tuple[tuple[re.Pattern, ModelCard], ...] = (
    # --- OpenAI ---
    (re.compile(r"^o[1-9](-|$)"), _f(Tier.STRONG, 200000, 100000, tools=None,
                                     notes="reasoning model; tool support varies by version")),
    (re.compile(r"^gpt-4[.\-]?1?-?(mini|nano)"), _f(Tier.STANDARD, 128000, 16384)),
    (re.compile(r"^gpt-4o-mini"), _f(Tier.STANDARD, 128000, 16384)),
    (re.compile(r"^gpt-4"), _f(Tier.STRONG, 128000, 16384)),
    (re.compile(r"^gpt-3\.5"), _f(Tier.WEAK, 16385, 4096)),
    # --- Anthropic ---
    (re.compile(r"^claude.*(opus|sonnet)"), _f(Tier.STRONG, 200000, 8192, json_obj=False)),
    (re.compile(r"^claude.*haiku"), _f(Tier.STANDARD, 200000, 8192, json_obj=False)),
    (re.compile(r"^claude"), _f(Tier.STANDARD, 200000, 8192, json_obj=False)),
    # --- Google ---
    (re.compile(r"^gemini.*flash-lite"), _f(Tier.WEAK, 1_048_576, 8192)),
    (re.compile(r"^gemini.*pro"), _f(Tier.STRONG, 1_048_576, 8192)),
    (re.compile(r"^gemini.*flash"), _f(
        Tier.STANDARD, 1_048_576, 8192,
        notes="function calling documented; unverified here through the OpenAI-compat endpoint")),
    (re.compile(r"^gemini"), _f(Tier.STANDARD, 1_048_576, 8192)),
    # --- Qwen (DashScope, Groq, OpenRouter, or local) ---
    (re.compile(r"^qwen.*max"), _f(Tier.STRONG, 131072, 8192)),
    (re.compile(r"^qwen.*plus"), _f(Tier.STANDARD, 131072, 8192)),
    (re.compile(r"^qwen.*turbo"), _f(Tier.WEAK, 131072, 8192)),
    (re.compile(r"^qwen.*coder"), _f(Tier.STANDARD, 131072, 8192)),
    # --- others reachable via OpenRouter / local ---
    (re.compile(r"^deepseek.*(reasoner|r1)"), _f(Tier.STRONG, 65536, 8192, tools=None,
                                                 notes="reasoning model; tool support varies")),
    (re.compile(r"^deepseek"), _f(Tier.STANDARD, 65536, 8192)),
    (re.compile(r"^mistral.*large"), _f(Tier.STRONG, 131072, 8192)),
    (re.compile(r"^(mistral|mixtral|ministral)"), _f(Tier.STANDARD, 32768, 8192)),
    (re.compile(r"^command-?r"), _f(Tier.STANDARD, 131072, 4096)),
    (re.compile(r"^grok"), _f(Tier.STRONG, 131072, 8192)),
)

# Open-weight families where the CAPABILITY is known but the TIER depends on the
# parameter count: a llama-3 at 8B and at 70B are the same family and very different
# models. Capabilities come from here, tier from the size token.
_OPEN_FAMILIES: tuple[tuple[re.Pattern, dict], ...] = (
    (re.compile(r"^llama-3"),
     {"native_tools": True, "json_object": True, "context_tokens": 131072,
      "max_output_tokens": 8192}),
    (re.compile(r"^qwen[\d.]"),
     {"native_tools": True, "json_object": True, "context_tokens": 131072,
      "max_output_tokens": 8192}),
    (re.compile(r"^(gemma|phi|granite)"),
     {"native_tools": None, "json_object": True, "context_tokens": 8192}),
)


# "…-72b-instruct" → 72. Anchored on a separator so the 2.5 in qwen2.5 is not a size.
_SIZE_RE = re.compile(r"[-_](\d+(?:\.\d+)?)\s*b(?:[-_]|$)")


def _tier_from_size(key: str) -> tuple[Tier, float] | None:
    """Infer tier from a parameter count in the model name.

    This is what lets an unseen open model — a new Qwen, a local Ollama build — route
    sensibly instead of falling to UNKNOWN. Deliberately caps at STANDARD: only a named
    family earns STRONG, because size alone does not prove frontier reasoning.
    """
    matches = _SIZE_RE.findall(key)
    if not matches:
        return None
    billions = max(float(m) for m in matches)
    if billions < 5:
        return Tier.TINY, billions
    if billions < 14:
        return Tier.WEAK, billions
    return Tier.STANDARD, billions


_PROVIDER_OVERLAYS: dict[str, dict] = {
    "openrouter": {
        "native_tools": None,
        "notes": "OpenRouter :free routes to varying upstreams — capability differs per request",
    },
}

_TIER_NAMES = {t.name.lower(): t for t in Tier}


def parse_tier_overrides(raw: str) -> dict[str, Tier]:
    """`"custom=standard,groq:llama-3.3-70b-versatile=strong"` → lookup table.

    Unparseable entries are skipped rather than raising: a typo in config should not
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
    """Best available knowledge about (provider, model).

    Every settings read uses getattr with a default — the existing LLM test fixtures
    pass a SimpleNamespace with a handful of attributes and must keep working.
    """
    key = _normalize(model)

    card = _MEASURED.get(key)

    if card is None:
        # Open families first: their capability is known, their tier is not.
        sized = _tier_from_size(key)
        for pattern, caps in _OPEN_FAMILIES:
            if pattern.match(key) and sized is not None:
                tier, billions = sized
                card = ModelCard("", tier, source="family+size", **caps,
                                 notes=f"{pattern.pattern} family at ~{billions:g}B")
                break

    if card is None:
        for pattern, family in _FAMILIES:
            if pattern.match(key):
                card = family
                break
    if card is None:
        sized = _tier_from_size(key)
        if sized is not None:
            tier, billions = sized
            card = ModelCard(
                "", tier, native_tools=None, source="size",
                notes=f"tier inferred from ~{billions:g}B parameters; "
                      f"set LLM_TIER_OVERRIDES to correct it")
    if card is None:
        card = ModelCard("", Tier.UNKNOWN, source="default",
                         notes="unrecognized model — set LLM_TIER_OVERRIDES to declare its tier")

    card = replace(card, model=model)

    overlay = _PROVIDER_OVERLAYS.get(provider.lower())
    if overlay:
        card = replace(card, **overlay)

    overrides = parse_tier_overrides(
        getattr(settings, "llm_tier_overrides", "") if settings is not None else "")
    for candidate in (f"{provider.lower()}:{model.lower()}", provider.lower()):
        if candidate in overrides:
            card = replace(card, tier=overrides[candidate], source="settings")
            break
    return card
