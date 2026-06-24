"""Central settings, loaded from environment / .env via pydantic-settings.

Secrets live only in .env (gitignored) or the process environment — never in code.
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # CI/CD sets unset secrets to "" (empty string). Treat blank as "not provided"
    # so optional int fields don't blow up parsing (e.g. TELEGRAM_CHAT_ID="").
    @field_validator(
        "telegram_api_id", "telegram_chat_id", "telegram_owner_id", mode="before"
    )
    @classmethod
    def _blank_int_to_none(cls, v):
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    # --- LLM: multi-provider with failover (see jobagent/llm_client.py) ---
    # Primary provider; the rest become automatic backups. Free providers stay as
    # backups even after you add a paid one and point LLM_PROVIDER at it.
    llm_provider: str = Field("groq", alias="LLM_PROVIDER")

    groq_api_key: str = Field("", alias="GROQ_API_KEY")
    openrouter_api_key: str = Field("", alias="OPENROUTER_API_KEY")
    openai_api_key: str = Field("", alias="OPENAI_API_KEY")
    gemini_api_key: str = Field("", alias="GEMINI_API_KEY")
    anthropic_api_key: str = Field("", alias="ANTHROPIC_API_KEY")

    # Per-provider model (sensible free defaults).
    groq_model: str = Field("llama-3.1-8b-instant", alias="GROQ_MODEL")
    openrouter_model: str = Field("meta-llama/llama-3.3-70b-instruct:free", alias="OPENROUTER_MODEL")
    openai_model: str = Field("gpt-4o-mini", alias="OPENAI_MODEL")
    gemini_model: str = Field("gemini-2.0-flash", alias="GEMINI_MODEL")
    anthropic_model: str = Field("claude-sonnet-4-6", alias="ANTHROPIC_MODEL")

    # Custom OpenAI-compatible endpoint (Ollama / vLLM / any local or hosted server).
    custom_llm_base_url: str = Field("", alias="CUSTOM_LLM_BASE_URL")
    custom_llm_api_key: str = Field("", alias="CUSTOM_LLM_API_KEY")
    custom_llm_model: str = Field("", alias="CUSTOM_LLM_MODEL")

    # v2.1 config UI: admin password (gates the config endpoints) + secret-store key.
    dashboard_password: str = Field("", alias="DASHBOARD_PASSWORD")
    master_key: str = Field("", alias="JOBAGENT_MASTER_KEY")
    cors_origins: str = Field("*", alias="JOBAGENT_CORS_ORIGINS")  # comma-separated; * = any (token auth)

    # Telegram — channel reader (Telethon)
    telegram_api_id: int | None = Field(None, alias="TELEGRAM_API_ID")
    telegram_api_hash: str = Field("", alias="TELEGRAM_API_HASH")
    telegram_phone: str = Field("", alias="TELEGRAM_PHONE")
    telegram_channels: str = Field("", alias="TELEGRAM_CHANNELS")  # comma-separated
    telegram_session: str = Field("data/telegram", alias="TELEGRAM_SESSION")
    telegram_fetch_limit: int = Field(50, alias="TELEGRAM_FETCH_LIMIT")
    # Telegram — bot you talk to (Bot API)
    telegram_bot_token: str = Field("", alias="TELEGRAM_BOT_TOKEN")
    # Destination/owner chat. TELEGRAM_CHAT_ID is where the bot DMs you the digest;
    # for a personal bot it equals your own user id. Kept as the canonical owner gate.
    telegram_chat_id: int | None = Field(None, alias="TELEGRAM_CHAT_ID")
    telegram_owner_id: int | None = Field(None, alias="TELEGRAM_OWNER_ID")

    @property
    def telegram_destination(self) -> int | None:
        """Where to send messages / whom to trust — chat_id wins, owner_id fallback."""
        return self.telegram_chat_id or self.telegram_owner_id

    # ATS boards to watch — comma-separated company slugs per platform.
    # e.g. GREENHOUSE_SLUGS=stripe,airbnb  LEVER_SLUGS=netflix  ASHBY_SLUGS=ramp
    greenhouse_slugs: str = Field("", alias="GREENHOUSE_SLUGS")
    lever_slugs: str = Field("", alias="LEVER_SLUGS")
    ashby_slugs: str = Field("", alias="ASHBY_SLUGS")

    # Aggregator (Indeed/LinkedIn/Glassdoor/JobRight)
    serpapi_key: str = Field("", alias="SERPAPI_KEY")
    apify_token: str = Field("", alias="APIFY_TOKEN")

    # Store
    db_path: str = Field("data/jobagent.db", alias="JOBAGENT_DB_PATH")

    # Email (Tier 1 apply)
    smtp_host: str = Field("", alias="SMTP_HOST")
    smtp_port: int = Field(587, alias="SMTP_PORT")
    smtp_user: str = Field("", alias="SMTP_USER")
    smtp_password: str = Field("", alias="SMTP_PASSWORD")
    apply_from_email: str = Field("", alias="APPLY_FROM_EMAIL")


def _build_effective() -> Settings:
    """Env/.env settings, then overlaid by the encrypted secret store (if present)."""
    s = Settings()
    try:
        from jobagent.secrets_store import SecretStore

        overlay = SecretStore().load()
    except Exception:  # noqa: BLE001 — missing key/crypto or unreadable store → env-only
        overlay = {}
    if overlay:
        fields = set(Settings.model_fields)
        update = {k: v for k, v in overlay.items() if k in fields and v not in (None, "")}
        # model_copy bypasses validation — coerce numeric fields the UI stores as strings.
        int_fields = {"telegram_chat_id", "telegram_api_id", "telegram_owner_id", "smtp_port"}
        for k in list(update):
            if k in int_fields and isinstance(update[k], str) and update[k].strip().lstrip("-").isdigit():
                update[k] = int(update[k])
        if update:
            s = s.model_copy(update=update)
    return s


_cached: Settings | None = None


def get_settings() -> Settings:
    global _cached
    if _cached is None:
        _cached = _build_effective()
    return _cached


def reload_settings() -> Settings:
    """Bust the cache so config-store edits take effect (call after a config write)."""
    global _cached
    _cached = None
    return get_settings()
