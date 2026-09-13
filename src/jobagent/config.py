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

    # The gate's age limit is a non-optional int where 0 means "no limit", so a blank
    # from the dashboard form (or CI) must become 0, not None.
    @field_validator("ingest_max_age_days", mode="before")
    @classmethod
    def _blank_int_to_zero(cls, v):
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return 0
        return v

    # Advanced apply-review flags: a blank from CI/env means "use the default" rather
    # than a parse error (bool("") and int("") both raise in pydantic).
    @field_validator("apply_review_enabled", "apply_render_cv_pdf",
                     "openrouter_free_fanout", "pollinations_enabled", mode="before")
    @classmethod
    def _blank_bool_false(cls, v):
        if isinstance(v, str) and v.strip() == "":
            return False
        return v

    @field_validator("openrouter_free_max", mode="before")
    @classmethod
    def _blank_free_max(cls, v):
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return 6
        return v

    @field_validator("apply_review_rounds", mode="before")
    @classmethod
    def _blank_rounds_to_one(cls, v):
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return 1
        return v

    # --- LLM: multi-provider with failover. These fields are read by agentkit's
    # LLMService (jobagent/llm_client.build_llm is a thin adapter over it). ---
    # Primary provider; the rest become automatic backups. Free providers stay as
    # backups even after you add a paid one and point LLM_PROVIDER at it.
    llm_provider: str = Field("groq", alias="LLM_PROVIDER")

    groq_api_key: str = Field("", alias="GROQ_API_KEY")
    openrouter_api_key: str = Field("", alias="OPENROUTER_API_KEY")
    openai_api_key: str = Field("", alias="OPENAI_API_KEY")
    gemini_api_key: str = Field("", alias="GEMINI_API_KEY")
    anthropic_api_key: str = Field("", alias="ANTHROPIC_API_KEY")
    # Qwen via DashScope's OpenAI-compatible endpoint (also reachable through
    # OpenRouter or a local server, which the custom provider covers).
    qwen_api_key: str = Field("", alias="QWEN_API_KEY")
    # Cerebras: ~1M tokens/day free, fastest inference available. OpenAI-compatible.
    cerebras_api_key: str = Field("", alias="CEREBRAS_API_KEY")
    # GitHub Models: free with a GitHub account. The credential is a PAT with the
    # `models:read` scope — not a vendor API key.
    github_models_token: str = Field("", alias="GITHUB_MODELS_TOKEN")
    # More OpenAI-compatible providers with free/generous tiers. All slot into the same
    # failover chain (llm_client._PROVIDERS); add a key and they activate in order.
    sambanova_api_key: str = Field("", alias="SAMBANOVA_API_KEY")   # fast Llama inference, free tier
    nvidia_api_key: str = Field("", alias="NVIDIA_API_KEY")         # NVIDIA NIM (integrate.api.nvidia.com)
    mistral_api_key: str = Field("", alias="MISTRAL_API_KEY")       # Mistral La Plateforme
    llama_api_key: str = Field("", alias="LLAMA_API_KEY")           # Meta Llama API (api.llama.com)

    # Per-provider model (sensible free defaults).
    # llama-3.3-70b-versatile over llama-3.1-8b-instant, measured on this project's own
    # tasks (spike, Aug 2026, n=5-6 against the real CV and store). Both free on Groq.
    #
    #   quality on current tasks : identical — 18/18 valid+conformant for both
    #   latency                  : 8b is FASTER (0.38s vs 0.61s median per scoring call),
    #                              worth ~6s across a whole pipeline run. Negligible.
    #   tool-calling loop        : 8b scores 0/5. It emits well-formed tool calls and
    #                              picks the right tool, then CANNOT use the tool result
    #                              to produce an answer. 70b: 5/5.
    #
    # So this is not a speed or quality choice — on today's prompt-and-parse work the 8B
    # is fine and marginally quicker. It is chosen because the assistant harness needs a
    # model that can complete a tool loop, and a default that silently can't would make
    # every agent task take the degraded path.
    groq_model: str = Field("openai/gpt-oss-20b", alias="GROQ_MODEL")
    # :free slugs are withdrawn without notice: meta-llama/llama-3.3-70b-instruct:free
    # 404'd, then its replacement openai/gpt-oss-20b:free did too (verified Sep 2026).
    # This is only the FALLBACK now — openrouter_free_fanout fetches the live free list and
    # tries them all, so a dead slug self-heals. minimax-m3:free verified live (1M ctx,
    # tools). If it 404s, the fan-out covers you; to refresh, list the current free slugs at
    # https://openrouter.ai/api/v1/models and pick one advertising "tools".
    openrouter_model: str = Field("minimax/minimax-m3:free", alias="OPENROUTER_MODEL")
    openai_model: str = Field("gpt-4o-mini", alias="OPENAI_MODEL")
    gemini_model: str = Field("gemini-flash-latest", alias="GEMINI_MODEL")
    anthropic_model: str = Field("claude-sonnet-4-6", alias="ANTHROPIC_MODEL")
    qwen_model: str = Field("qwen-plus", alias="QWEN_MODEL")
    cerebras_model: str = Field("llama-3.3-70b", alias="CEREBRAS_MODEL")
    github_models_model: str = Field("openai/gpt-4o-mini", alias="GITHUB_MODELS_MODEL")
    # Defaults are best-effort current free/low-cost slugs — override per each provider's
    # model list if one 404s (the same way OpenRouter slugs rot).
    sambanova_model: str = Field("Meta-Llama-3.3-70B-Instruct", alias="SAMBANOVA_MODEL")
    nvidia_model: str = Field("meta/llama-3.3-70b-instruct", alias="NVIDIA_MODEL")
    mistral_model: str = Field("mistral-small-latest", alias="MISTRAL_MODEL")
    llama_model: str = Field("Llama-3.3-70B-Instruct", alias="LLAMA_MODEL")
    pollinations_model: str = Field("openai", alias="POLLINATIONS_MODEL")

    # OpenRouter free-model fan-out: fetch the live list of `:free` chat models and try
    # them ALL as failover backends, so a withdrawn slug is just skipped for the next one.
    # ON by default — it is the resilient behavior (verified: gated models 403, the router
    # falls through to a working one), the fetch is cached hourly, and it degrades to the
    # single configured model if the list can't be fetched. Only matters with an OpenRouter
    # key. Set false to pin the single OPENROUTER_MODEL instead.
    openrouter_free_fanout: bool = Field(True, alias="OPENROUTER_FREE_FANOUT")
    openrouter_free_max: int = Field(6, alias="OPENROUTER_FREE_MAX")   # cap the fan-out
    # Pollinations (text.pollinations.ai) needs no API key. Opt-in so a no-key install
    # still reports "no LLM configured" instead of silently routing through a third party.
    pollinations_enabled: bool = Field(False, alias="POLLINATIONS_ENABLED")

    # Custom OpenAI-compatible endpoint (Ollama / vLLM / any local or hosted server).
    custom_llm_base_url: str = Field("", alias="CUSTOM_LLM_BASE_URL")
    custom_llm_api_key: str = Field("", alias="CUSTOM_LLM_API_KEY")
    custom_llm_model: str = Field("", alias="CUSTOM_LLM_MODEL")

    # v2.1 config UI: admin password (gates the config endpoints) + secret-store key.
    dashboard_password: str = Field("", alias="DASHBOARD_PASSWORD")
    # Gate GET routes too. Default False preserves the historical posture, which is
    # correct on a laptop: the API binds 127.0.0.1 and the dashboard renders reads
    # server-side with no token to offer. Turn it ON whenever the API is reachable
    # from anywhere else — /applications and /followups reveal where you applied,
    # what was rejected, and where you are interviewing. See SECURITY.md.
    require_auth_reads: bool = Field(False, alias="JOBAGENT_REQUIRE_AUTH_READS")

    # Bounded requests per hour, per client, per class. Generous by default — these
    # exist to stop a runaway loop or an exposed port draining an LLM quota, not to
    # police normal single-user work. 0 disables a class.
    rate_limit_enabled: bool = Field(True, alias="JOBAGENT_RATE_LIMIT_ENABLED")
    rate_limit_assistant: int = Field(60, alias="JOBAGENT_RATE_LIMIT_ASSISTANT")
    rate_limit_ingest: int = Field(20, alias="JOBAGENT_RATE_LIMIT_INGEST")
    rate_limit_write: int = Field(600, alias="JOBAGENT_RATE_LIMIT_WRITE")
    # Safety valve for an exposed deployment. 0 = unlimited, which is the default
    # because the purge UI already shows an exact count and requires a second click —
    # consent is obtained before the delete, so a cap would only add friction.
    max_purge_rows: int = Field(0, alias="JOBAGENT_MAX_PURGE_ROWS")
    master_key: str = Field("", alias="JOBAGENT_MASTER_KEY")
    # Comma-separated browser origins allowed to call the API. Defaults to the local
    # dashboard only; set explicitly when the dashboard is deployed elsewhere. "*" is
    # honored if you set it deliberately, but is not the default — an open origin plus
    # a reachable port is how a stranger drives your apply endpoints.
    cors_origins: str = Field(
        "http://localhost:1234,http://127.0.0.1:1234", alias="JOBAGENT_CORS_ORIGINS"
    )

    # --- Application review (drafter → reviewer → revise; see apply/flow.py) --------
    # A second agent critiques the tailored CV/cover letter and the drafter revises.
    # It REWRITES generated content, so it ships OFF: turn it on only after a live-model
    # check that the revise prompt still honors R1 (no fabrication) — see R1b. The
    # always-on ATS-parseability report (apply/verify.py) is independent of this flag.
    apply_review_enabled: bool = Field(False, alias="APPLY_REVIEW_ENABLED")
    apply_review_rounds: int = Field(1, alias="APPLY_REVIEW_ROUNDS")
    # Render the tailored CV to a PDF and attach THAT (so the ATS report checks the exact
    # bytes that get sent), instead of the static profile.cv_path. Needs fpdf2 (the
    # `apply` extra). Ships OFF: it changes what is attached, and an auto-rendered CV is
    # plainer than a hand-designed one — keep your own PDF unless you want this.
    apply_render_cv_pdf: bool = Field(False, alias="APPLY_RENDER_CV_PDF")

    # --- Ingest gate (dashboard-editable; see ingestion/gate.py) -------------------
    # Applied between fetch and store, so filtered postings never enter the store and
    # never cost matching time on later passes. Comma-separated lists; all blank/0 =
    # store everything (the pre-gate behavior).
    ingest_max_age_days: int = Field(0, alias="INGEST_MAX_AGE_DAYS")     # 0 = no age limit
    ingest_locations: str = Field("", alias="INGEST_LOCATIONS")          # e.g. remote,EMEA,Africa
    ingest_drop_keywords: str = Field("", alias="INGEST_DROP_KEYWORDS")
    # Sources allowed to run. Blank = fall back to [sources] in preferences.toml.
    ingest_sources: str = Field("", alias="INGEST_SOURCES")

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
    # RapidAPI key for JSearch — LinkedIn/Indeed/Glassdoor behind one endpoint.
    jsearch_api_key: str = Field("", alias="JSEARCH_API_KEY")
    # Blank means "search where the profile says you are"; set to override.
    jsearch_location: str = Field("", alias="JSEARCH_LOCATION")
    apify_token: str = Field("", alias="APIFY_TOKEN")

    # Store
    db_path: str = Field("data/jobagent.db", alias="JOBAGENT_DB_PATH")

    # Email (Tier 1 apply)
    smtp_host: str = Field("", alias="SMTP_HOST")
    smtp_port: int = Field(587, alias="SMTP_PORT")
    smtp_user: str = Field("", alias="SMTP_USER")
    smtp_password: str = Field("", alias="SMTP_PASSWORD")
    apply_from_email: str = Field("", alias="APPLY_FROM_EMAIL")

    # Inbox outcome detection (optional). Reads the mailbox you apply from and PROPOSES
    # status changes for one-tap confirmation — it never applies one. Off unless a host
    # is set; most people will use the same account as SMTP above.
    imap_host: str = Field("", alias="IMAP_HOST")
    imap_port: int = Field(993, alias="IMAP_PORT")
    imap_user: str = Field("", alias="IMAP_USER")
    imap_password: str = Field("", alias="IMAP_PASSWORD")
    imap_folder: str = Field("INBOX", alias="IMAP_FOLDER")


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
        int_fields = {"telegram_chat_id", "telegram_api_id", "telegram_owner_id", "smtp_port",
                      "ingest_max_age_days"}
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
