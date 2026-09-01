"""Multi-provider LLM client with automatic failover.

One `.complete(system, user, json_mode=False)` interface, backed by an ordered chain
of providers. The primary (settings.llm_provider) is tried first; on any error —
rate limit, quota exhausted, timeout, auth, outage — it falls through to the next
provider that has an API key. Add a paid provider and point LLM_PROVIDER at it and it
becomes primary while the free ones remain backups; remove/exhaust it and the free
ones keep serving. Order is stable and free-first among backups.

Groq / OpenRouter / OpenAI / Gemini are OpenAI-API-compatible (Gemini via its OpenAI
endpoint), so one backend serves all four. Anthropic uses its own SDK. SDKs are
imported lazily, so this module imports fine without them and tests can inject fakes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
import re

logger = logging.getLogger("jobagent.llm")

# One registry, one source of truth. Every provider here is OpenAI-API-compatible except
# anthropic. `key`/`model` name the Settings attributes read via getattr, so an install
# without a field (or a test with a partial SimpleNamespace) simply skips that provider.
# `keyless` providers (Pollinations) need no API key but are opt-in — see build_llm.
# base_url=None means the OpenAI SDK default. Endpoints verified against the user's list.
_PROVIDERS = {
    "groq":        {"base_url": "https://api.groq.com/openai/v1", "kind": "openai", "key": "groq_api_key", "model": "groq_model"},
    "gemini":      {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "kind": "openai", "key": "gemini_api_key", "model": "gemini_model"},
    "cerebras":    {"base_url": "https://api.cerebras.ai/v1", "kind": "openai", "key": "cerebras_api_key", "model": "cerebras_model"},
    "openrouter":  {"base_url": "https://openrouter.ai/api/v1", "kind": "openai", "key": "openrouter_api_key", "model": "openrouter_model"},
    "sambanova":   {"base_url": "https://api.sambanova.ai/v1", "kind": "openai", "key": "sambanova_api_key", "model": "sambanova_model"},
    "nvidia":      {"base_url": "https://integrate.api.nvidia.com/v1", "kind": "openai", "key": "nvidia_api_key", "model": "nvidia_model"},
    "mistral":     {"base_url": "https://api.mistral.ai/v1", "kind": "openai", "key": "mistral_api_key", "model": "mistral_model"},
    "llama":       {"base_url": "https://api.llama.com/compat/v1", "kind": "openai", "key": "llama_api_key", "model": "llama_model"},
    "github":      {"base_url": "https://models.github.ai/inference", "kind": "openai", "key": "github_models_token", "model": "github_models_model"},
    "pollinations": {"base_url": "https://text.pollinations.ai/openai", "kind": "openai", "model": "pollinations_model", "keyless": True},
    "openai":      {"base_url": None, "kind": "openai", "key": "openai_api_key", "model": "openai_model"},
    "anthropic":   {"kind": "anthropic", "key": "anthropic_api_key", "model": "anthropic_model"},
}
# Backups are tried in this order (free/fast first), after the primary. Keyless/paid
# providers are in the list but only activate under the conditions build_llm enforces.
_DEFAULT_ORDER = ["groq", "cerebras", "gemini", "openrouter", "sambanova", "nvidia",
                  "mistral", "llama", "github", "pollinations", "custom", "openai", "anthropic"]


# --- OpenRouter free-model discovery --------------------------------------------
# Free model slugs rot (openai/gpt-oss-20b:free 404'd once it lost its free tier), so we
# never hardcode them. We fetch the live list, filter to free text-chat models, and try
# them all as failover backends. Cached per process; on any fetch failure the caller
# falls back to the single configured model, so this can only ever add resilience.
_FREE_CACHE_TTL = 3600.0
_free_cache: dict = {"at": -1e18, "ids": None}


def _rank_free(models: list[dict]) -> list[str]:
    """Free `:free` text→text chat models, tool-capable and larger-context first."""
    def is_chat(m: dict) -> bool:
        a = m.get("architecture") or {}
        return "text" in (a.get("input_modalities") or []) and "text" in (a.get("output_modalities") or [])
    free = [m for m in models
            if str(m.get("id", "")).endswith(":free") and is_chat(m)]
    free.sort(key=lambda m: (1 if "tools" in (m.get("supported_parameters") or []) else 0,
                             m.get("context_length") or 0), reverse=True)
    return [str(m["id"]) for m in free]


def openrouter_free_models(api_key: str = "", *, limit: int = 6, timeout: float = 8.0,
                           _clock=None) -> list[str]:
    """Live list of OpenRouter free chat-model ids (ranked). Cached for an hour; returns
    [] on any failure so callers degrade to the configured model rather than break."""
    import time
    now = (_clock or time.monotonic)()
    cached = _free_cache["ids"]
    if cached is not None and (now - _free_cache["at"]) < _FREE_CACHE_TTL:
        return cached[:limit]
    try:
        import httpx
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        data = httpx.get("https://openrouter.ai/api/v1/models",
                         headers=headers, timeout=timeout).json()["data"]
        ids = _rank_free(data)
    except Exception as exc:  # noqa: BLE001 — network/parse; resilience, never a hard fail
        logger.warning("OpenRouter free-model fetch failed (%s); using the configured model", exc)
        ids = []
    _free_cache.update(at=now, ids=ids)
    return ids[:limit]


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z0-9]*\n?", "", t)
    t = re.sub(r"\n?```$", "", t)
    return t.strip()


class OpenAICompatBackend:
    """Groq / OpenRouter / OpenAI / Gemini via the OpenAI SDK + a base_url."""

    def __init__(self, name: str, api_key: str, model: str, base_url: str | None, temperature: float):
        self.name = name
        self._api_key = api_key
        self.model = model
        self._base_url = base_url
        self._temperature = temperature
        self._client = None

    def _ensure(self):
        if self._client is None:
            from openai import OpenAI  # lazy
            self._client = OpenAI(api_key=self._api_key, base_url=self._base_url)
        return self._client

    def generate(self, system: str, user: str) -> str:
        resp = self._ensure().chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=self._temperature,
        )
        return resp.choices[0].message.content or ""


class AnthropicBackend:
    def __init__(self, name: str, api_key: str, model: str, temperature: float):
        self.name = name
        self._api_key = api_key
        self.model = model
        self._temperature = temperature
        self._client = None

    def _ensure(self):
        if self._client is None:
            import anthropic  # lazy
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def generate(self, system: str, user: str) -> str:
        resp = self._ensure().messages.create(
            model=self.model, max_tokens=4096, temperature=self._temperature,
            system=system, messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


@dataclass
class LLMUsage:
    """Approximate spend, tracked per process.

    The backends return a string and nothing else, so real token counts are not
    available without changing every provider's return type. These are ESTIMATES from
    character length (the conventional ~4 chars per token) and are named as such
    everywhere they surface — a number presented as billed usage when it is a guess is
    worse than no number, because it gets trusted.

    Useful for what it is actually for: comparing tasks and providers against each
    other, and noticing that a chain's first backend fails on every call.
    """

    calls: int = 0
    failures: int = 0
    prompt_chars: int = 0
    completion_chars: int = 0
    by_provider: dict = field(default_factory=dict)

    CHARS_PER_TOKEN = 4

    def record(self, provider: str, prompt: str, completion: str) -> None:
        self.calls += 1
        self.prompt_chars += len(prompt or "")
        self.completion_chars += len(completion or "")
        slot = self.by_provider.setdefault(provider, {"calls": 0, "failures": 0, "chars": 0})
        slot["calls"] += 1
        slot["chars"] += len(prompt or "") + len(completion or "")

    def record_failure(self, provider: str) -> None:
        self.failures += 1
        slot = self.by_provider.setdefault(provider, {"calls": 0, "failures": 0, "chars": 0})
        slot["failures"] += 1

    @property
    def estimated_tokens(self) -> int:
        return (self.prompt_chars + self.completion_chars) // self.CHARS_PER_TOKEN

    def as_dict(self) -> dict:
        """Shape written to the run ledger. `estimated` is in the key name on purpose."""
        return {
            "calls": self.calls,
            "failures": self.failures,
            "estimated_tokens": self.estimated_tokens,
            "estimated_prompt_tokens": self.prompt_chars // self.CHARS_PER_TOKEN,
            "estimated_completion_tokens": self.completion_chars // self.CHARS_PER_TOKEN,
            "by_provider": self.by_provider,
        }


class MultiLLM:
    """Ordered failover over backends. Each backend exposes `.name` and
    `.generate(system, user) -> str`."""

    def __init__(self, backends: list):
        if not backends:
            raise ValueError("MultiLLM needs at least one backend")
        self.backends = backends
        self.last_provider: str | None = None
        # Per-process usage, for "which task is draining my quota" rather than billing.
        self.usage = LLMUsage()

    @property
    def chain(self) -> list[str]:
        return [b.name for b in self.backends]

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        if json_mode:
            system = system + "\nReturn ONLY valid JSON — no markdown, no code fences, no prose."
        errors = []
        for backend in self.backends:
            try:
                text = backend.generate(system, user)
                if not text.strip():
                    raise RuntimeError("empty response")
                self.last_provider = backend.name
                self.usage.record(backend.name, system + user, text)
                if backend is not self.backends[0]:
                    logger.warning("LLM failover: served by '%s'", backend.name)
                return _strip_fences(text) if json_mode else text
            except Exception as exc:  # noqa: BLE001 — that's the whole point: try the next one
                # Failures are counted too. A provider that fails on every call still
                # costs latency, and a chain whose first backend is dead is invisible
                # otherwise — which is exactly how two dead model slugs went unnoticed.
                self.usage.record_failure(backend.name)
                errors.append(f"{backend.name}: {type(exc).__name__}: {exc}")
                logger.warning("LLM provider '%s' failed, trying next — %s", backend.name, exc)
        raise RuntimeError("All LLM providers failed:\n  " + "\n  ".join(errors))


def _openrouter_backends(api_key: str, configured_model: str, temperature: float, settings) -> list:
    """Fan out over OpenRouter's live free models when OPENROUTER_FREE_FANOUT is on, so
    failover walks every currently-free model. Falls back to the single configured model
    if fan-out is off or the model list can't be fetched."""
    base = _PROVIDERS["openrouter"]["base_url"]
    if not getattr(settings, "openrouter_free_fanout", False):
        return [OpenAICompatBackend("openrouter", api_key, configured_model, base, temperature)]
    limit = getattr(settings, "openrouter_free_max", 6) or 6
    ids = openrouter_free_models(api_key, limit=limit)
    if not ids:
        return [OpenAICompatBackend("openrouter", api_key, configured_model, base, temperature)]
    return [OpenAICompatBackend(f"openrouter:{mid.rsplit('/', 1)[-1].replace(':free', '')}",
                                api_key, mid, base, temperature) for mid in ids]


def build_llm(settings, temperature: float = 0.3) -> MultiLLM | None:
    """Construct the failover chain from settings. Returns None if no provider is usable.

    Table-driven off `_PROVIDERS`: every provider with a key (Pollinations needs none but
    is opt-in) joins the chain in `_DEFAULT_ORDER`, primary first. OpenRouter can fan out
    across all currently-free models — see `_openrouter_backends`."""
    primary = settings.llm_provider if settings.llm_provider in (*_PROVIDERS, "custom") else "groq"
    order = [primary] + [p for p in _DEFAULT_ORDER if p != primary]

    backends: list = []
    seen: set = set()
    for name in order:
        if name in seen:
            continue
        seen.add(name)
        if name == "custom":
            # Custom OpenAI-compatible endpoint (Ollama/vLLM/etc.) — enabled by a base_url;
            # api_key may be blank for local servers (the OpenAI client needs a placeholder).
            custom_base = getattr(settings, "custom_llm_base_url", "")
            if not custom_base:
                continue
            backends.append(OpenAICompatBackend(
                "custom", getattr(settings, "custom_llm_api_key", "") or "not-needed",
                getattr(settings, "custom_llm_model", "") or "default", custom_base, temperature))
            continue
        spec = _PROVIDERS.get(name)
        if not spec:
            continue
        key = getattr(settings, spec["key"], "") if spec.get("key") else ""
        if spec.get("keyless"):
            # Keyless providers are opt-in, so a no-key install still returns None.
            if not getattr(settings, f"{name}_enabled", False):
                continue
            key = key or "not-needed"
        elif not key:
            continue
        model = getattr(settings, spec["model"], "") or ""
        if spec["kind"] == "anthropic":
            backends.append(AnthropicBackend(name, key, model, temperature))
        elif name == "openrouter":
            backends.extend(_openrouter_backends(key, model, temperature, settings))
        else:
            backends.append(OpenAICompatBackend(name, key, model, spec["base_url"], temperature))
    if not backends:
        return None
    logger.info("LLM chain: %s", " → ".join(b.name for b in backends))
    return MultiLLM(backends)
