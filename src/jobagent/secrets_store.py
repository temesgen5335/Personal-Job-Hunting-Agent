"""Encrypted, UI-editable secret/config store (v2.1).

Holds the runtime-editable settings (LLM keys/provider/models, Telegram tokens, SMTP,
custom model endpoint) in a Fernet-encrypted file, separate from `.env`. The dashboard
edits it through the auth-gated API; `config.get_settings()` overlays it on top of the
env so every component (api/bot/pipeline) sees the same effective config.

Encryption key comes from `JOBAGENT_MASTER_KEY` (a Fernet key). No file → empty overlay
and no crypto dependency needed, so a base install is unaffected.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Fields the config UI may set (must be real Settings field names).
MANAGED_FIELDS = [
    "llm_provider",
    "groq_api_key", "openrouter_api_key", "openai_api_key", "gemini_api_key", "anthropic_api_key",
    "qwen_api_key", "cerebras_api_key", "github_models_token",
    "groq_model", "openrouter_model", "openai_model", "gemini_model", "anthropic_model",
    "qwen_model", "cerebras_model", "github_models_model",
    "custom_llm_base_url", "custom_llm_api_key", "custom_llm_model",
    "telegram_bot_token", "telegram_chat_id", "telegram_api_id", "telegram_api_hash",
    "telegram_phone", "telegram_channels",
    "smtp_host", "smtp_port", "smtp_user", "smtp_password", "apply_from_email",
    # Ingest gate + source selection (non-secret, but same UI-editable surface).
    "ingest_max_age_days", "ingest_locations", "ingest_drop_keywords", "ingest_sources",
]
# Fields masked in any read-back (never return the plaintext value).
SECRET_FIELDS = {
    "groq_api_key", "openrouter_api_key", "openai_api_key", "gemini_api_key",
    "anthropic_api_key", "qwen_api_key", "cerebras_api_key", "github_models_token",
    "custom_llm_api_key", "telegram_bot_token",
    "telegram_api_hash", "smtp_password",
}


def _from_env_or_dotenv(name: str, default: str = "") -> str:
    """Read a setting from the process environment, falling back to `.env`.

    Everything else in this project reads config through pydantic-settings, which loads
    `.env` into a Settings object but deliberately does NOT export those values into
    `os.environ`. This module used `os.environ` alone, so a key set in `.env` — the
    location `.env.example` documents — never reached it: reads appeared to work (an
    absent store needs no crypto) while every *write* failed with "not set", even though
    it was set. That silently broke the Settings page, the assistant's config tool and
    rollback for anyone following the documented setup.

    `get_settings()` cannot be called from here: `config._build_effective()` constructs
    a SecretStore, so this would recurse forever. `dotenv_values` is the parser
    pydantic-settings itself uses, which keeps the two readings from drifting.
    """
    # PRESENCE, not truthiness. A test (or an operator) that sets the variable to an
    # empty string means "explicitly no key" and must not have `.env` silently supplied
    # underneath — that is how a suite stops being hermetic and starts depending on the
    # developer's own machine. Falling back only when the variable is *absent* keeps the
    # override honest in both directions.
    if name in os.environ:
        return os.environ[name] or default
    try:
        from dotenv import dotenv_values

        return (dotenv_values(".env").get(name) or default)
    except Exception:  # noqa: BLE001 — no dotenv, or an unreadable file: fall back
        return default


class SecretStore:
    def __init__(self, path: str | None = None, key: str | bytes | None = None):
        self.path = Path(path or _from_env_or_dotenv("JOBAGENT_SECRETS_PATH",
                                                     "data/secrets.enc"))
        self._key = key or _from_env_or_dotenv("JOBAGENT_MASTER_KEY")

    @staticmethod
    def generate_key() -> str:
        from cryptography.fernet import Fernet
        return Fernet.generate_key().decode()

    def _fernet(self):
        from cryptography.fernet import Fernet
        if not self._key:
            raise RuntimeError("JOBAGENT_MASTER_KEY not set — required to read/write the secret store.")
        key = self._key.encode() if isinstance(self._key, str) else self._key
        return Fernet(key)

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> dict:
        """Decrypted dict, or {} if no store file (no crypto needed in that case)."""
        if not self.exists():
            return {}
        return json.loads(self._fernet().decrypt(self.path.read_bytes()))

    def save(self, values: dict) -> None:
        clean = {k: v for k, v in values.items() if k in MANAGED_FIELDS}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(self._fernet().encrypt(json.dumps(clean).encode()))

    def update(self, patch: dict) -> dict:
        """Merge a patch (only MANAGED_FIELDS) into the store. Empty-string clears a field."""
        current = self.load()
        for k, v in patch.items():
            if k not in MANAGED_FIELDS:
                continue
            if v in (None, ""):
                current.pop(k, None)
            else:
                current[k] = v
        self.save(current)
        return current


def masked_view(effective: dict) -> dict:
    """Render MANAGED_FIELDS for the UI: secrets as {'set': bool}, others as values."""
    out: dict = {}
    for f in MANAGED_FIELDS:
        val = effective.get(f)
        out[f] = {"set": bool(val)} if f in SECRET_FIELDS else val
    return out
