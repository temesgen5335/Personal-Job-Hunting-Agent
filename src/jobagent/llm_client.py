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
import re

logger = logging.getLogger("jobagent.llm")

# base_url=None means the SDK's default (OpenAI). kind selects the backend class.
_PROVIDERS = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "kind": "openai"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "kind": "openai"},
    "openai": {"base_url": None, "kind": "openai"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "kind": "openai"},
    "anthropic": {"kind": "anthropic"},
}
# Backups are tried in this order (free/fast first), after the primary.
_DEFAULT_ORDER = ["groq", "gemini", "openrouter", "openai", "anthropic"]


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


class MultiLLM:
    """Ordered failover over backends. Each backend exposes `.name` and
    `.generate(system, user) -> str`."""

    def __init__(self, backends: list):
        if not backends:
            raise ValueError("MultiLLM needs at least one backend")
        self.backends = backends
        self.last_provider: str | None = None

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
                if backend is not self.backends[0]:
                    logger.warning("LLM failover: served by '%s'", backend.name)
                return _strip_fences(text) if json_mode else text
            except Exception as exc:  # noqa: BLE001 — that's the whole point: try the next one
                errors.append(f"{backend.name}: {type(exc).__name__}: {exc}")
                logger.warning("LLM provider '%s' failed, trying next — %s", backend.name, exc)
        raise RuntimeError("All LLM providers failed:\n  " + "\n  ".join(errors))


def build_llm(settings, temperature: float = 0.3) -> MultiLLM | None:
    """Construct the failover chain from settings. Returns None if no provider has a key."""
    keys = {
        "groq": settings.groq_api_key,
        "openrouter": settings.openrouter_api_key,
        "openai": settings.openai_api_key,
        "gemini": settings.gemini_api_key,
        "anthropic": settings.anthropic_api_key,
    }
    models = {
        "groq": settings.groq_model,
        "openrouter": settings.openrouter_model,
        "openai": settings.openai_model,
        "gemini": settings.gemini_model,
        "anthropic": settings.anthropic_model,
    }
    primary = settings.llm_provider if settings.llm_provider in _PROVIDERS else "groq"
    order = [primary] + [p for p in _DEFAULT_ORDER if p != primary]

    backends = []
    for name in order:
        if not keys.get(name):
            continue
        spec = _PROVIDERS[name]
        if spec["kind"] == "anthropic":
            backends.append(AnthropicBackend(name, keys[name], models[name], temperature))
        else:
            backends.append(OpenAICompatBackend(name, keys[name], models[name], spec["base_url"], temperature))
    if not backends:
        return None
    logger.info("LLM chain: %s", " → ".join(b.name for b in backends))
    return MultiLLM(backends)
