"""Application configuration.

All configuration comes from the environment (12-factor). No secret ever has a
non-empty default, and every secret-typed field is validated to be absent from
source control. Settings are loaded once and passed explicitly; modules never
read ``os.environ`` at import time.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .enums import Locale, NetworkPolicy, OperatingMode, RuntimeKind


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

    # --- Trigger scheduler -------------------------------------------------
    #: Whether a worker process runs the cron scheduler loop.
    scheduler_enabled: bool = True
    #: How often the scheduler scans for due triggers.
    scheduler_interval_seconds: float = Field(default=15.0, gt=0)
    #: How long a scheduler's claim on a trigger is valid (prevents two
    #: schedulers from firing the same occurrence).
    scheduler_lease_seconds: int = Field(default=60, ge=5)
    #: An occurrence is "on time" if it is discovered within this many seconds;
    #: beyond it the misfire policy applies.
    scheduler_misfire_grace_seconds: int = Field(default=300, ge=0)
    #: Upper bound on missed occurrences run in one catch-up (RUN_ALL policy).
    scheduler_max_catchup: int = Field(default=10, ge=1, le=1000)
    #: Event types a scheduler/API process may accept via ``POST /events``.
    #: Empty means "accept any event type" (useful for local/dev); production
    #: should list the known internal events.
    allowed_event_types: tuple[str, ...] = ()
    #: Maximum clock skew tolerated on a signed webhook delivery.
    webhook_tolerance_seconds: int = Field(default=300, ge=1)

    # --- Model gateway -----------------------------------------------------
    # API keys are secrets; they must be injected via environment, never code.
    openai_api_key: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")
    default_model: str = "openai/gpt-4o-mini"
    # Local OpenAI-compatible endpoint (Ollama / llama.cpp / vLLM) for offline.
    local_model_base_url: str = ""
    local_model_name: str = "qwen2.5:7b"
    request_timeout_seconds: float = 60.0

    # --- Local model runtime -----------------------------------------------
    # Which local inference backend to use: "llama_cpp" (in-process, default),
    # "ollama" (local server), or "openai_compatible" (any local endpoint).
    local_model_backend: str = "llama_cpp"
    # Path to the weights file for a file-backed backend (llama_cpp). Must live
    # inside `local_model_storage_dir`; the runtime refuses to load outside it.
    local_model_path: str = ""
    # Approved directory that holds local model files. Downloads and loads are
    # confined to this directory.
    local_model_storage_dir: str = ""
    # Optional expected SHA-256 of the weights file (integrity check).
    local_model_sha256: str = ""
    # Context window requested from the local model.
    local_model_context_window: int = Field(default=4096, ge=128)
    # GPU layers to offload (0 = CPU only). Never auto-detected into a promise.
    local_model_n_gpu_layers: int = Field(default=0, ge=0)
    # Inference threads (0 = let the backend choose from the CPU count).
    local_model_n_threads: int = Field(default=0, ge=0)
    # Run the file-backed engine in a dedicated child process. A native crash in
    # the inference library then surfaces as a recoverable error instead of
    # taking the agent down. Default on: containment is the safe choice.
    local_model_isolate: bool = True
    # Timeout for a single local inference call, in seconds. CPU-only inference
    # of a planning+tool-schema prompt can legitimately take minutes on a
    # contended or low-core host, so the default is generous; the per-*run*
    # deadline (max_run_seconds) is the real guard against a stuck request.
    local_model_timeout_seconds: float = Field(default=300.0, gt=0)
    # Routing: prefer the local model even when a remote one is available.
    model_prefer_local: bool = False
    # Routing: whether falling back to a remote model is permitted at all.
    # Set false to pin every request to the local model (privacy mode).
    model_allow_remote_fallback: bool = True

    # --- Local / desktop runtime -------------------------------------------
    #: Declared operating mode for the local runtime: online/offline/hybrid.
    local_mode: OperatingMode = OperatingMode.OFFLINE
    #: Directories the local agent may touch. Empty means "workspace only".
    #: These are an explicit allow-list; the sandbox refuses everything else.
    local_workspace_roots: tuple[str, ...] = ()
    #: Directories explicitly refused even if under an allowed root.
    local_denied_roots: tuple[str, ...] = ()
    #: Outbound network policy for local tools. Default is restrictive.
    local_network_policy: NetworkPolicy = NetworkPolicy.DISABLED
    #: Hosts permitted when the network policy is ALLOWLIST.
    local_network_allowlist: tuple[str, ...] = ()
    #: Commands the local runtime may run. Empty means "nothing runs" — the
    #: safe default. Entries are program names or absolute paths.
    local_command_allowlist: tuple[str, ...] = ()
    #: Whether confidential data may be routed to a remote model at all.
    local_allow_confidential_remote: bool = False
    #: Human-friendly label for the enrolled device (never a hardware id).
    local_device_label: str = "this-device"
    #: Secure local control API. Bound to loopback by default; never public.
    local_control_enabled: bool = False
    local_control_host: str = "127.0.0.1"
    local_control_port: int = Field(default=8765, ge=1, le=65535)
    #: Shared secret the desktop client presents to the local control API.
    #: Injected via environment only; never stored in source control.
    local_control_token: SecretStr = SecretStr("")

    # --- Computer use / perception (Phase 7) -------------------------------
    #: Whether the local runtime may control the machine (screen capture, pointer,
    #: keyboard, windows). Off by default: computer control is opt-in, and even
    #: when on, every action is still permission- and approval-gated.
    computer_control_enabled: bool = False
    #: Which computer backend to use. ``auto`` picks the platform backend
    #: (X11 on Linux); an unavailable backend is reported honestly.
    computer_backend: str = "auto"
    #: OCR languages for local perception, in order (Arabic-first). Accepts a
    #: comma-separated environment value (e.g. ``ara,eng``).
    computer_ocr_languages: Annotated[tuple[str, ...], NoDecode] = ("ara", "eng")
    #: How sensitive screen content is: ``never_store`` (discard, never remote),
    #: ``local_only`` (default; local use, never remote) or ``cloud_allowed``.
    computer_screen_policy: str = "local_only"
    #: Longest side a capture is downscaled to before perception/vision.
    computer_max_image_dimension: int = Field(default=1600, ge=64)
    #: Whether an action must reference a fresh observation.
    computer_require_fresh_observation: bool = True
    #: Maximum age (seconds) of an observation an action may act on.
    computer_max_observation_age_seconds: float = Field(default=30.0, gt=0)
    #: Per-run caps on computer actions (anti-runaway).
    computer_max_pointer_actions: int = Field(default=60, ge=1)
    computer_max_keyboard_actions: int = Field(default=60, ge=1)
    computer_max_captures: int = Field(default=40, ge=1)

    # --- Browser / web (Phase 8) -------------------------------------------
    #: Whether the agent may use the web through the browser tools. Off by
    #: default; when on, every action is still permission- and approval-gated.
    browser_enabled: bool = False
    #: Which browser backend to use: ``auto`` (Playwright/Chromium when present),
    #: ``playwright`` or ``null``. An unavailable backend is reported honestly.
    browser_backend: str = "auto"
    #: Whether the browser runs headless. Headed is for desktop supervision.
    browser_headless: bool = True
    #: Optional explicit Chromium/Chrome executable path (else auto-detected).
    browser_executable_path: str = ""
    #: How sensitive page content is: ``never_store``, ``local_only`` (default)
    #: or ``cloud_allowed``.
    browser_content_policy: str = "local_only"
    #: When non-empty, only these domains (and subdomains) may be opened.
    browser_allowed_domains: Annotated[tuple[str, ...], NoDecode] = ()
    #: Domains that are always refused.
    browser_blocked_domains: Annotated[tuple[str, ...], NoDecode] = ()
    #: Whether private/loopback/link-local hosts may be opened. Off by default;
    #: a controlled local test site turns it on deliberately.
    browser_allow_local_addresses: bool = False
    #: Whether ``file://`` URLs may be opened (local test sites), and the roots
    #: they must resolve inside.
    browser_allow_file_urls: bool = False
    browser_allowed_file_roots: Annotated[tuple[str, ...], NoDecode] = ()
    #: Longest side a page screenshot is downscaled to before vision.
    browser_max_image_dimension: int = Field(default=1600, ge=64)
    #: Per-run caps on browser actions (anti-runaway).
    browser_max_navigations: int = Field(default=40, ge=1)
    browser_max_interactions: int = Field(default=80, ge=1)
    browser_max_observations: int = Field(default=60, ge=1)
    browser_max_tabs: int = Field(default=8, ge=1)
    #: Maximum size of a single downloaded file, in bytes.
    browser_max_download_bytes: int = Field(default=64 * 1024 * 1024, ge=1)
    #: Default navigation/action timeout, in milliseconds.
    browser_timeout_ms: int = Field(default=15_000, ge=100, le=120_000)
    #: Whether an action must reference a fresh observation.
    browser_require_fresh_observation: bool = True
    browser_max_observation_age_seconds: float = Field(default=60.0, gt=0)
    #: Whether submitting a form is treated as consequential and needs approval.
    browser_require_approval_for_submit: bool = True

    # --- Agent limits (guard rails against infinite loops) ------------------
    max_steps_per_run: int = Field(default=40, ge=1, le=500)
    max_tool_retries: int = Field(default=3, ge=0, le=10)
    max_replans: int = Field(default=3, ge=0, le=10)
    max_run_seconds: float = Field(default=600.0, gt=0)
    max_context_tokens: int = Field(default=16_000, ge=512)
    #: Safety ceiling on tool schemas advertised per call. The token budget is
    #: the real limiter (it counts each tool's full JSON schema and drops
    #: optional tools that do not fit), so this only guards against a
    #: mis-configured window. 0 means "use the context default".
    max_tools_per_call: int = Field(default=64, ge=0, le=64)

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

    @field_validator("allowed_event_types", mode="before")
    @classmethod
    def _split_event_types(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(e.strip() for e in value.split(",") if e.strip())
        return value

    @field_validator(
        "local_workspace_roots",
        "local_denied_roots",
        "local_network_allowlist",
        "local_command_allowlist",
        "computer_ocr_languages",
        "browser_allowed_domains",
        "browser_blocked_domains",
        "browser_allowed_file_roots",
        mode="before",
    )
    @classmethod
    def _split_paths(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(v.strip() for v in value.split(",") if v.strip())
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
