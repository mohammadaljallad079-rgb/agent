"""Composition root.

Wires the platform together from configuration. Nothing else in the codebase
constructs its own dependencies; the container is the single place where
concrete implementations are chosen, which keeps the agent core testable (tests
build a container with a deterministic model provider and a temp workspace).

Persistence selection is deliberate and boundary-aware:

* With ``AAP_DATABASE_URL`` set, durable Postgres stores are used (cloud/prod).
* Without it, process-local in-memory stores are used (offline/desktop/tests).
* With ``AAP_REDIS_URL`` set, distributed rate limiting, the reliable job queue
  and the idempotency cache are used; otherwise local in-memory equivalents are.

The agent core depends only on the store/queue ABCs, never on these choices.
"""

from __future__ import annotations

import logging

import redis.asyncio as aioredis

from .agent.executor import ToolExecutor
from .agent.runner import AgentRunner
from .audit.base import AuditLog
from .audit.logging_sink import LoggingAuditSink
from .audit.memory_sink import InMemoryAuditSink
from .context.manager import ContextManager
from .core.config import Settings
from .core.enums import Locale
from .core.errors import ConfigurationError
from .infrastructure.idempotency import (
    IdempotencyGuard,
    InMemoryIdempotencyGuard,
    RedisIdempotencyGuard,
)
from .infrastructure.rate_limit import InMemoryRateLimiter, RateLimiterBackend, RedisRateLimiter
from .infrastructure.redis import build_redis_client
from .integrations.base import IntegrationRegistry
from .integrations.webhook import WebhookIntegration
from .memory.base import MemoryStore
from .memory.memory_store import InMemoryMemoryStore
from .memory.policy import MemoryPolicy
from .models.gateway import ModelGateway
from .models.providers.openai_compatible import OpenAICompatibleProvider
from .models.registry import ProviderRegistry
from .observability.logging import configure_logging
from .observability.metrics import Metrics
from .observability.otel import otel_exporter
from .observability.readiness import (
    AuditProbe,
    DatabaseProbe,
    ModelProbe,
    ReadinessProbe,
    ReadinessService,
    RedisProbe,
)
from .observability.tracing import Exporter, Tracer, configure_tracing
from .permissions.approvals import ApprovalStore, InMemoryApprovalStore
from .permissions.policy import PolicyEngine
from .persistence.database import Database
from .persistence.stores import (
    PostgresApprovalStore,
    PostgresAuditStore,
    PostgresMemoryStore,
    PostgresRunStore,
    PostgresTaskStore,
)
from .runs.store import InMemoryRunStore, RunStore
from .runtime.boundary import RuntimeProfile
from .tasks.engine import TaskEngine
from .tasks.store import InMemoryTaskStore, TaskStore
from .tools.base import Tool
from .tools.builtin import (
    EchoTool,
    ListDirectoryTool,
    ReadFileTool,
    ShellCommandTool,
    WriteFileTool,
)
from .tools.builtin.echo import FinishTool
from .tools.registry import ToolRegistry
from .worker.queue import InMemoryJobQueue, JobQueue, RedisJobQueue

logger = logging.getLogger(__name__)


class Platform:
    """A fully-wired platform instance.

    Holds every long-lived component and produces configured
    :class:`AgentRunner` instances.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        gateway: ModelGateway,
        tools: ToolRegistry,
        policy: PolicyEngine,
        approvals: ApprovalStore,
        audit: AuditLog,
        memory: MemoryStore,
        memory_policy: MemoryPolicy,
        tasks: TaskEngine,
        context: ContextManager,
        executor: ToolExecutor,
        integrations: IntegrationRegistry,
        runtime: RuntimeProfile,
        runs: RunStore,
        queue: JobQueue,
        rate_limiter: RateLimiterBackend,
        idempotency: IdempotencyGuard,
        database: Database | None = None,
        redis_client: aioredis.Redis | None = None,
        metrics: Metrics | None = None,
        tracer: Tracer | None = None,
        readiness: ReadinessService | None = None,
    ) -> None:
        self.settings = settings
        self.gateway = gateway
        self.tools = tools
        self.policy = policy
        self.approvals = approvals
        self.audit = audit
        self.memory = memory
        self.memory_policy = memory_policy
        self.tasks = tasks
        self.context = context
        self.executor = executor
        self.integrations = integrations
        self.runtime = runtime
        self.runs = runs
        self.queue = queue
        self.rate_limiter = rate_limiter
        self.idempotency = idempotency
        self.database = database
        self.redis_client = redis_client
        self.metrics = metrics or Metrics()
        self.tracer = tracer or configure_tracing(
            enabled=settings.otel_enabled, service=settings.otel_service_name
        )
        self.readiness = readiness or ReadinessService(
            [], timeout_seconds=settings.readiness_timeout_seconds
        )

    @property
    def durable(self) -> bool:
        """Whether this platform is backed by durable Postgres persistence."""
        return self.database is not None

    @property
    def distributed(self) -> bool:
        """Whether Redis-backed coordination (queue/rate limit/idempotency) is on."""
        return self.redis_client is not None

    def build_runner(self, *, locale: Locale | None = None) -> AgentRunner:
        """Create an agent runner bound to this platform's components."""
        return AgentRunner(
            gateway=self.gateway,
            tools=self.tools,
            executor=self.executor,
            tasks=self.tasks,
            context=self.context,
            memory=self.memory,
            memory_policy=self.memory_policy,
            audit=self.audit,
            approvals=self.approvals,
            settings=self.settings,
            locale=locale or self.settings.default_locale,
        )

    async def aclose(self) -> None:
        await self.gateway.aclose()
        await self.integrations.aclose()
        await self.audit.aclose()
        await self.queue.aclose()
        await self.idempotency.aclose()
        await self.rate_limiter.aclose()
        if self.redis_client is not None:
            await self.redis_client.aclose()
        if self.database is not None:
            await self.database.dispose()

    @classmethod
    def build(
        cls,
        settings: Settings | None = None,
        *,
        extra_tools: list[Tool] | None = None,
        provider_registry: ProviderRegistry | None = None,
        default_model: str | None = None,
        fallbacks: tuple[str, ...] = (),
        memory_store: MemoryStore | None = None,
        task_store: TaskStore | None = None,
        approval_store: ApprovalStore | None = None,
        run_store: RunStore | None = None,
        audit_log: AuditLog | None = None,
        queue: JobQueue | None = None,
        database: Database | None = None,
        redis_client: aioredis.Redis | None = None,
        metrics: Metrics | None = None,
        tracer: Tracer | None = None,
    ) -> Platform:
        """Construct a platform from settings.

        By default the model provider is built from credentials and the method
        raises :class:`ConfigurationError` when none is configured — the
        platform will not start in a half-working state where every request
        fails. Callers may instead inject a prepared ``provider_registry`` (used
        by tests and by the desktop runtime, which supplies a local model).
        """
        settings = settings or Settings()
        runtime = (
            RuntimeProfile.local()
            if settings.runtime_kind.value == "local"
            else RuntimeProfile.cloud()
        )

        # --- Observability -------------------------------------------------
        if settings.log_json:
            configure_logging(
                level=getattr(logging, settings.log_level.upper(), logging.INFO),
                service=settings.otel_service_name,
                environment=settings.environment,
            )
        metrics = metrics or Metrics()
        if tracer is None:
            exporter: Exporter | None = None
            if settings.otel_enabled:
                exporter = otel_exporter(settings.otel_service_name)
            tracer = configure_tracing(
                enabled=True, service=settings.otel_service_name, exporter=exporter
            )

        # --- Model providers -------------------------------------------
        if provider_registry is not None:
            if default_model is None:
                raise ConfigurationError(
                    "default_model is required when injecting a provider registry"
                )
            gateway = ModelGateway(
                provider_registry,
                settings=settings,
                default_model=default_model,
                fallbacks=fallbacks,
                metrics=metrics,
                tracer=tracer,
            )
        else:
            registry = ProviderRegistry()
            candidates: list[str] = []
            if settings.openai_api_key.get_secret_value():
                registry.register(
                    OpenAICompatibleProvider(
                        name="openai",
                        api_key=settings.openai_api_key.get_secret_value(),
                        timeout=settings.request_timeout_seconds,
                    )
                )
                model = (
                    settings.default_model
                    if settings.default_model.startswith("openai/")
                    else "openai/gpt-4o-mini"
                )
                candidates.append(model)
            if settings.local_model_base_url:
                registry.register(
                    OpenAICompatibleProvider(
                        name="local",
                        base_url=settings.local_model_base_url,
                        api_key=None,
                        timeout=settings.request_timeout_seconds,
                    )
                )
                candidates.append(f"local/{settings.local_model_name}")

            if not candidates:
                raise ConfigurationError(
                    "no model provider configured: set AAP_OPENAI_API_KEY or "
                    "AAP_LOCAL_MODEL_BASE_URL (offline/local model)"
                )
            gateway = ModelGateway(
                registry,
                settings=settings,
                default_model=candidates[0],
                fallbacks=tuple(candidates[1:]),
                metrics=metrics,
                tracer=tracer,
            )
        logger.info("model gateway ready: default=%s", gateway.default_model)

        # --- Persistence selection -------------------------------------
        if database is None and settings.database_url.get_secret_value():
            database = Database(
                settings.database_url.get_secret_value(),
                pool_size=settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
            )
        logger.info(
            "durable persistence: %s",
            "postgres" if database is not None else "in-memory (local runtime)",
        )

        # --- Redis / coordination selection ----------------------------
        if redis_client is None and settings.redis_url.get_secret_value():
            redis_client = build_redis_client(settings.redis_url.get_secret_value())
        logger.info(
            "coordination: %s",
            "redis (distributed queue/rate limit/idempotency)"
            if redis_client is not None
            else "in-process (local runtime)",
        )

        # --- Tools ------------------------------------------------------
        tools = ToolRegistry(
            [
                EchoTool(),
                FinishTool(),
                ReadFileTool(),
                WriteFileTool(),
                ListDirectoryTool(),
                ShellCommandTool(),
                *(extra_tools or []),
            ]
        )

        # --- Governance -------------------------------------------------
        policy = PolicyEngine()
        if database is not None:
            approvals: ApprovalStore = approval_store or PostgresApprovalStore(database)
            memory: MemoryStore = memory_store or PostgresMemoryStore(database)
            runs: RunStore = run_store or PostgresRunStore(database)
            selected_task_store: TaskStore = task_store or PostgresTaskStore(database)
        else:
            approvals = approval_store or InMemoryApprovalStore()
            memory = memory_store or InMemoryMemoryStore()
            runs = run_store or InMemoryRunStore()
            selected_task_store = task_store or InMemoryTaskStore()

        if audit_log is not None:
            audit = audit_log
        elif database is not None:
            # Durable audit plus a structured log line for streaming/SIEM.
            audit = AuditLog([PostgresAuditStore(database), LoggingAuditSink()])
        else:
            audit = AuditLog([InMemoryAuditSink(), LoggingAuditSink()])

        memory_policy = MemoryPolicy.default(tenant_id="__platform__")

        # --- Tasks & context -------------------------------------------
        tasks = TaskEngine(selected_task_store)
        context = ContextManager(settings)

        # --- Coordination primitives -----------------------------------
        if redis_client is not None:
            redis = redis_client
            rate_limiter: RateLimiterBackend = RedisRateLimiter(
                redis, rate_per_minute=settings.rate_limit_per_minute
            )
            idempotency: IdempotencyGuard = RedisIdempotencyGuard(redis)
            job_queue: JobQueue = queue or RedisJobQueue(redis)
        else:
            rate_limiter = InMemoryRateLimiter(rate_per_minute=settings.rate_limit_per_minute)
            idempotency = InMemoryIdempotencyGuard()
            job_queue = queue or InMemoryJobQueue()

        executor = ToolExecutor(
            registry=tools,
            policy=policy,
            approvals=approvals,
            audit=audit,
            approval_ttl_seconds=settings.approval_ttl_seconds,
            idempotency=idempotency,
            metrics=metrics,
            tracer=tracer,
        )

        # --- Readiness probes ------------------------------------------
        probes: list[ReadinessProbe] = [ModelProbe(gateway), AuditProbe(audit)]
        if database is not None:
            probes.append(DatabaseProbe(database))
        if redis_client is not None:
            probes.append(RedisProbe(redis_client))
        readiness = ReadinessService(probes, timeout_seconds=settings.readiness_timeout_seconds)

        # --- Integrations ----------------------------------------------
        integrations = IntegrationRegistry()
        integrations.register(WebhookIntegration(name="generic_webhook"))

        return cls(
            settings=settings,
            gateway=gateway,
            tools=tools,
            policy=policy,
            approvals=approvals,
            audit=audit,
            memory=memory,
            memory_policy=memory_policy,
            tasks=tasks,
            context=context,
            executor=executor,
            integrations=integrations,
            runtime=runtime,
            runs=runs,
            queue=job_queue,
            rate_limiter=rate_limiter,
            idempotency=idempotency,
            database=database,
            redis_client=redis_client,
            metrics=metrics,
            tracer=tracer,
            readiness=readiness,
        )

    def memory_policy_for(self, tenant_id: str) -> MemoryPolicy:
        """Per-tenant memory policy (defaults, extensible per tenant)."""
        return MemoryPolicy.default(tenant_id=tenant_id)
