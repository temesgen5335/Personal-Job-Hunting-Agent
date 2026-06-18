"""Thin OpenRouter (OpenAI-compatible) chat client shared by generation features.

Duck-typed: anything with `.complete(system, user, json_mode=False) -> str` can stand
in for it (tests pass a fake). `from_settings` returns None when no key is configured,
so callers can degrade gracefully.
"""

from __future__ import annotations


class LLMClient:
    def __init__(self, api_key: str, model: str, temperature: float = 0.3):
        self.api_key = api_key
        self.model = model
        self.temperature = temperature

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        from openai import OpenAI  # lazy: optional [llm] extra

        client = OpenAI(api_key=self.api_key, base_url="https://openrouter.ai/api/v1")
        kwargs = {}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=self.temperature,
            **kwargs,
        )
        return resp.choices[0].message.content or ""


def from_settings(settings) -> LLMClient | None:
    if not settings.openrouter_api_key:
        return None
    return LLMClient(settings.openrouter_api_key, settings.llm_model)
