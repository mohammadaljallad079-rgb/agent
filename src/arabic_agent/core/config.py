"""Application configuration.

All configuration comes from the environment (12-factor). No secret ever has a
non-empty default, and every secret-typed field is validated to be absent from
source control. Settings are loaded once and passed explicitly; modules never
read ``os.environ`` at import time.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .enums import Locale, RuntimeKind


class Settings(BaseSettings):
    """Process-wide configuration."""

    model_config = SettingsConfigDict(
        env_prefix="AAP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Environment -------------------------------------------------------
    environment: Literal["dev", "test", "staging", "prod"] = "dev"
    runtime_kind: RuntimeKind = RuntimeKind.CLOUD
    debug: bool = False

    # --- Localization ------------------------------------------------------
    default_locale: Locale = Locale.AR
    supported_locales: tuple[Locale, ...] = (Locale.AR, Locale.EN)

    # --- HTTP surface ------------------------------------------------------
    host: str = "0.0.0.0"  # noqa: S104 - container/desktop runtime binds all interfaces
    port: int = 8000
    cors_origins: tuple[str, ...] = ()

    # --- Storage -----------------------------------------------------------
    # Postgres for multi-tenant deployments; SQLite for local/offline runtime.
    database_url: SecretStr = SecretStr("")
    # A local, offline-first store path used by the local runtime.
    local_store_dir: str = ".agent-data"
    # Connection pool sizing for the async Postgres engine.
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=200)

    # --- Ephemeral coordination (Redis) ------------------------------------
    # Redis is used ONLY for rate limiting, the job queue, locks and the
    # idempotency cache. Postgres remains the durable source of truth.
    redis_url: SecretStr = SecretStr("")

    # --- Execution mode ----------------------------------------------------
    # "inline": the API process runs the agent (local/offline runtime, tests).
    # "queued": the API enqueues a job and a separate worker executes it
    #           (production/cloud runtime).
    execution_mode: Literal["inline", "queued"] = "inline"

    # --- Worker / queue ----------------------------------------------------
    worker_id: str = ""
    worker_poll_seconds: float = Field(default=1.0, gt=0)
    #: How long a worker lease is valid before the run is considered stale.
    worker_lease_seconds: int = Field(default=120, ge=5)
    #: How often a running worker renews its lease.
    worker_heartbeat_seconds: int = Field(default=20, ge=1)
    #: Maximum job delivery attempts before a run is marked failed.
    job_max_attempts: int = Field(default=3, ge=1, le=20)
    job_backoff_seconds: float = Field(default=5.0, ge=0)
    #: How often the recovery loop scans for stale runs.
    recovery_interval_seconds: float = Field(default=15.0, gt=0)

    # --- Model gateway -----------------------------------------------------
    # API keys are secrets; they must be injected via environment, never code.
    openai_api_key: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")
    default_model: str = "openai/gpt-4o-mini"
    # Local OpenAI-compatible endpoint (Ollama / llama.cpp / vLLM) for offline.
    local_model_base_url: str = ""
    local_model_name: str = "qwen2.5:7b"
    request_timeout_seconds: float = 60.0

    # --- Agent limits (guard rails against infinite loops) ------------------
    max_steps_per_run: int = Field(default=40, ge=1, le=500)
    max_tool_retries: int = Field(default=3, ge=0, le=10)
    max_replans: int = Field(default=3, ge=0, le=10)
    max_run_seconds: float = Field(default=600.0, gt=0)
    max_context_tokens: int = Field(default=16_000, ge=512)

    # --- Security ----------------------------------------------------------
    approval_ttl_seconds: int = Field(default=3600, ge=30)
    rate_limit_per_minute: int = Field(default=120, ge=1)
    require_auth: bool = True
    # JSON object mapping API key -> {tenant_id, user_id, roles, permissions}.
    # Injected via environment only; never stored in source control.
    api_keys: SecretStr = SecretStr("")

    # --- Observability -----------------------------------------------------
    # Structured JSON logs with correlation ids. On by default; disable only
    # for a human-readable local console.
    log_json: bool = True
    log_level: str = "INFO"
    # In-process metrics exposed at /metrics (Prometheus text format).
    metrics_enabled: bool = True
    # Tracing is recorded in-process; the OpenTelemetry exporter is used only
    # when the SDK is installed and this is enabled.
    otel_enabled: bool = False
    otel_service_name: str = "arabic-agent"
    # Timeout for each dependency readiness probe.
    readiness_timeout_seconds: float = Field(default=2.0, gt=0)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(o.strip() for o in value.split(",") if o.strip())
        return value

    @field_validator("supported_locales", mode="before")
    @classmethod
    def _split_locales(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(Locale(v.strip()) for v in value.split(",") if v.strip())
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "prod"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings singleton.

    Tests override configuration by clearing this cache and setting env vars.
    """
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache (used by tests and reload endpoints)."""
    get_settings.cache_clear()
