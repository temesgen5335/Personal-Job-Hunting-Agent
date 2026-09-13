"""Assemble the backend chain from configuration.

Without this, callers hand-construct backends and every new provider means editing
every call site. Here, adding a provider means adding one descriptor — and adding a
*key* means nothing at all: the chain picks it up.

Provider descriptors are data, not code, so a host application can extend or replace
the table without forking anything. Nothing here imports a provider SDK; the backends
do that lazily, so an install with no SDKs still resolves a chain (it just cannot call
it).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentkit.llm.capabilities import resolve_card


@dataclass(frozen=True)
class ProviderSpec:
    """How to reach one provider, and where its settings live.

    `key_field`/`model_field` are attribute names read off a settings object with
    getattr, so this stays decoupled from any particular config class.
    """

    name: str
    key_field: str
    model_field: str
    base_url: str | None = None
    kind: str = "openai"            # "openai" (OpenAI-compatible) | "anthropic"
    default_model: str = ""
    base_url_field: str = ""        # for self-hosted / custom endpoints
    requires_key: bool = True       # local servers often need none
    enabled_field: str = ""         # opt-in gate: keyless providers stay off unless truthy


# Every provider the harness can speak to. All the OpenAI-compatible ones share one
# backend — the only differences are the base URL and where the credentials live.
DEFAULT_PROVIDERS: tuple[ProviderSpec, ...] = (
    # Verified live Aug 2026 against the account's own /models list. The previous
    # default, llama-3.3-70b-versatile, had been withdrawn from Groq's catalogue and
    # 404'd on every call — the third time a pinned slug has died here.
    ProviderSpec("groq", "groq_api_key", "groq_model",
                 "https://api.groq.com/openai/v1",
                 default_model="openai/gpt-oss-20b"),
    # A "-latest" alias rather than a pinned version: gemini-2.0-flash was retired and
    # took the whole provider down with it. An alias survives a rotation by design,
    # which is worth more here than pinning a known quantity.
    ProviderSpec("gemini", "gemini_api_key", "gemini_model",
                 "https://generativelanguage.googleapis.com/v1beta/openai/",
                 default_model="gemini-flash-latest"),
    ProviderSpec("openai", "openai_api_key", "openai_model",
                 None, default_model="gpt-4o-mini"),
    ProviderSpec("anthropic", "anthropic_api_key", "anthropic_model",
                 None, kind="anthropic", default_model="claude-sonnet-4-6"),
    ProviderSpec("qwen", "qwen_api_key", "qwen_model",
                 "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                 default_model="qwen-plus"),
    ProviderSpec("openrouter", "openrouter_api_key", "openrouter_model",
                 "https://openrouter.ai/api/v1",
                 # :free slugs are withdrawn without notice — gpt-oss-20b:free 404'd once
                 # it lost its free tier. This is only the FALLBACK: with openrouter_free_fanout
                 # set, build_chain fans out over the live free list (openrouter.free_models),
                 # so a dead slug self-heals. minimax-m3:free verified live Sep 2026.
                 default_model="minimax/minimax-m3:free"),
    ProviderSpec("cerebras", "cerebras_api_key", "cerebras_model",
                 "https://api.cerebras.ai/v1",
                 default_model="llama-3.3-70b"),
    # GitHub Models: free with a GitHub account, OpenAI-compatible, fronts several
    # vendors. The credential is a PAT with the `models:read` scope, not a vendor key.
    ProviderSpec("github", "github_models_token", "github_models_model",
                 "https://models.github.ai/inference",
                 default_model="openai/gpt-4o-mini"),
    # More OpenAI-compatible providers with free/generous tiers. Endpoints only; the
    # caller supplies keys and (if desired) model overrides.
    ProviderSpec("sambanova", "sambanova_api_key", "sambanova_model",
                 "https://api.sambanova.ai/v1", default_model="Meta-Llama-3.3-70B-Instruct"),
    ProviderSpec("nvidia", "nvidia_api_key", "nvidia_model",
                 "https://integrate.api.nvidia.com/v1", default_model="meta/llama-3.3-70b-instruct"),
    ProviderSpec("mistral", "mistral_api_key", "mistral_model",
                 "https://api.mistral.ai/v1", default_model="mistral-small-latest"),
    ProviderSpec("llama", "llama_api_key", "llama_model",
                 "https://api.llama.com/compat/v1", default_model="Llama-3.3-70B-Instruct"),
    # Pollinations needs NO API key. Opt-in via `pollinations_enabled` so a keyless install
    # still builds an empty chain instead of silently routing through a third party.
    ProviderSpec("pollinations", "", "pollinations_model",
                 "https://text.pollinations.ai/openai", default_model="openai",
                 requires_key=False, enabled_field="pollinations_enabled"),
    ProviderSpec("custom", "custom_llm_api_key", "custom_llm_model",
                 None, base_url_field="custom_llm_base_url", requires_key=False),
)

# Tried in this order after the configured primary. Free and fast first, so a paid key
# is a deliberate escalation rather than a surprise on the bill.
DEFAULT_ORDER = ("groq", "cerebras", "gemini", "github", "openrouter", "sambanova",
                 "nvidia", "mistral", "llama", "qwen", "pollinations", "custom",
                 "openai", "anthropic")


@dataclass
class ChainReport:
    """What was built and what was skipped. Returned alongside the chain so a
    diagnostic can explain an empty or short chain instead of just showing one."""

    backends: list = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def __str__(self) -> str:
        lines = [f"{b.name}/{b.model}  {b.card.tier.name.lower()}"
                 f"  tools={b.card.native_tools} loop={b.card.tool_loop}"
                 f"  [{b.card.source}]" for b in self.backends] or ["(no usable provider)"]
        lines += [f"skipped {n}: {why}" for n, why in self.skipped]
        return "\n".join(lines)


def _make_backend(spec: ProviderSpec, api_key: str, model: str, base_url: str | None):
    # Imported here so this module loads with no provider SDK present.
    if spec.kind == "anthropic":
        from agentkit.llm.backends.anthropic_chat import AnthropicChat
        return AnthropicChat(spec.name, api_key, model)
    from agentkit.llm.backends.openai_compat import OpenAICompatChat
    return OpenAICompatChat(spec.name, api_key, model, base_url)


def build_chain(settings, *, primary: str = "", providers=DEFAULT_PROVIDERS,
                order=DEFAULT_ORDER, report: bool = False):
    """Build the ordered backend chain from whatever is configured.

    A provider appears if it has a key (or does not need one). Order is the configured
    primary first, then `order`. Note this only *orders* the chain — the router decides
    admission per task, so a primary that cannot do a job is skipped with a reason
    rather than silently attempted.
    """
    by_name = {p.name: p for p in providers}
    primary = (primary or getattr(settings, "llm_provider", "") or "").lower()
    sequence = ([primary] if primary in by_name else []) + [n for n in order if n != primary]

    out = ChainReport()
    for name in sequence:
        spec = by_name.get(name)
        if spec is None:
            continue
        if spec.enabled_field and not getattr(settings, spec.enabled_field, False):
            out.skipped.append((name, f"{spec.enabled_field} not set"))
            continue
        api_key = getattr(settings, spec.key_field, "") if spec.key_field else ""
        api_key = api_key or ""
        base_url = spec.base_url
        if spec.base_url_field:
            base_url = getattr(settings, spec.base_url_field, "") or None
            if not base_url:
                out.skipped.append((name, f"no {spec.base_url_field} set"))
                continue
        if spec.requires_key and not api_key:
            out.skipped.append((name, f"no {spec.key_field}"))
            continue

        # OpenRouter fan-out: try every currently-free model as a failover backend, so a
        # withdrawn slug is just skipped for the next. Opt-in; degrades to the single
        # configured model when off or when the live list can't be fetched.
        if name == "openrouter" and getattr(settings, "openrouter_free_fanout", False):
            from agentkit.llm.openrouter import free_models
            ids = free_models(api_key, limit=getattr(settings, "openrouter_free_max", 6) or 6)
            if ids:
                for mid in ids:
                    be = _make_backend(spec, api_key or "not-needed", mid, base_url)
                    be.name = f"openrouter:{mid.rsplit('/', 1)[-1].replace(':free', '')}"
                    be.card = resolve_card("openrouter", mid, settings)
                    out.backends.append(be)
                continue

        model = getattr(settings, spec.model_field, "") or spec.default_model
        if not model:
            out.skipped.append((name, f"no {spec.model_field} and no default"))
            continue

        backend = _make_backend(spec, api_key or "not-needed", model, base_url)
        backend.card = resolve_card(name, model, settings)
        out.backends.append(backend)

    return out if report else out.backends
