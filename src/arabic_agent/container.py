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
import secrets
from pathlib import Path

import redis.asyncio as aioredis

from .agent.executor import ToolExecutor
from .agent.runner import AgentRunner
from .audit.base import AuditLog
from .audit.logging_sink import LoggingAuditSink
from .audit.memory_sink import InMemoryAuditSink
from .browser.attachments import BrowserAttachmentProvider
from .browser.backends import build_browser_backend
from .browser.boundary import WebContentPolicy, WebDataBoundary
from .browser.policy import BrowserPolicy
from .browser.runtime import BrowserRuntime
from .browser.url_security import UrlPolicy
from .computer.attachments import ObservationAttachmentProvider
from .computer.backends import build_computer_backend
from .computer.ocr import build_ocr_engine
from .computer.perception import PerceptionLayer
from .computer.policy import ComputerPolicy
from .computer.runtime import ComputerRuntime
from .computer.screen_boundary import ScreenDataBoundary, ScreenDataPolicy
from .connectors.config import build_connectors, parse_connector_specs
from .connectors.credentials import CredentialResolver
from .connectors.ledger import InMemoryActionLedger
from .connectors.runtime import ConnectorRuntime
from .context.attachments import AttachmentProvider
from .context.manager import ContextManager
from .core.config import Settings
from .core.enums import Locale, ModelCapability, OperatingMode
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
from .local.adapters import detect_platform
from .local.boundary import DataBoundaryPolicy
from .local.device import DeviceRegistry, InMemoryDeviceRegistry, enroll_device
from .local.model import (
    LocalBackendProvider,
    OpenAICompatibleLocalProvider,
    build_backend,
)
from .local.model_registry import (
    LocalModelDescriptor,
    model_logical_id,
)
from .local.model_security import ensure_within_storage, resolve_storage_dir
from .local.persistence import LocalDatabase
from .local.persistence.database import default_store_path
from .local.persistence.stores import (
    LocalActionLedger,
    LocalApprovalStore,
    LocalAuditSink,
    LocalIdempotencyGuard,
    LocalPrintJobStore,
    LocalRunStore,
    LocalSyncQueue,
    LocalWorkflowRunStore,
    LocalWorkflowStore,
    LocalWorkflowVersionStore,
)
from .local.runtime import LocalRuntime, build_local_runtime
from .local.sync import InMemorySyncQueue, SyncCoordinator, SyncQueue
from .memory.base import MemoryStore
from .memory.memory_store import InMemoryMemoryStore
from .memory.policy import MemoryPolicy
from .models.gateway import ModelGateway
from .models.providers.anthropic_compatible import AnthropicCompatibleProvider
from .models.providers.openai_compatible import OpenAICompatibleProvider
from .models.providers.setup_required import SETUP_MODEL_ID, SetupRequiredProvider
from .models.registry import ProviderRegistry
from .models.routing import ModelRouter, RoutingPolicy
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
    PostgresTriggerRunStore,
    PostgresTriggerStore,
    PostgresWorkflowRunStore,
    PostgresWorkflowStore,
    PostgresWorkflowVersionStore,
)
from .printing.backends import build_print_backend
from .printing.policy import PrintPolicy
from .printing.runtime import PrintRuntime
from .printing.store import InMemoryPrintJobStore, PrintJobStore
from .runs.store import InMemoryRunStore, RunStore
from .runtime.boundary import RuntimeProfile
from .tasks.engine import TaskEngine
from .tasks.store import InMemoryTaskStore, TaskStore
from .tools.base import Tool
from .tools.builtin import (
    BROWSER_TOOLS,
    COMPUTER_TOOLS,
    CONNECTOR_TOOLS,
    DOCUMENT_GENERATION_TOOLS,
    DOCUMENT_TOOLS,
    EXTENDED_FILESYSTEM_TOOLS,
    PRINT_TOOLS,
    PROJECT_TOOLS,
    EchoTool,
    ListDirectoryTool,
    ReadFileTool,
    ShellCommandTool,
    WriteFileTool,
)
from .tools.builtin.echo import FinishTool
from .tools.builtin.local import LOCAL_TOOLS
from .tools.registry import ToolRegistry
from .triggers.events import EventBus
from .triggers.service import TriggerService
from .triggers.store import (
    InMemoryTriggerRunStore,
    InMemoryTriggerStore,
    TriggerRunStore,
    TriggerStore,
)
from .worker.queue import InMemoryJobQueue, JobQueue, RedisJobQueue
from .workflows.actions import ActionRegistry, default_actions
from .workflows.engine import WorkflowEngine
from .workflows.store import (
    InMemoryWorkflowRunStore,
    InMemoryWorkflowStore,
    InMemoryWorkflowVersionStore,
    WorkflowRunStore,
    WorkflowStore,
    WorkflowVersionStore,
)

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
        workflows: WorkflowStore,
        workflow_versions: WorkflowVersionStore,
        workflow_runs: WorkflowRunStore,
        workflow_engine: WorkflowEngine,
        workflow_actions: ActionRegistry,
        triggers: TriggerStore,
        trigger_runs: TriggerRunStore,
        trigger_service: TriggerService,
        events: EventBus,
        sync_queue: SyncQueue,
        local_runtime: LocalRuntime | None = None,
        local_model_provider: LocalBackendProvider | None = None,
        computer_runtime: ComputerRuntime | None = None,
        browser_runtime: BrowserRuntime | None = None,
        print_runtime: PrintRuntime | None = None,
        connector_runtime: ConnectorRuntime | None = None,
        attachment_provider: AttachmentProvider | None = None,
        database: Database | None = None,
        local_database: LocalDatabase | None = None,
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
        self.workflows = workflows
        self.workflow_versions = workflow_versions
        self.workflow_runs = workflow_runs
        self.workflow_engine = workflow_engine
        self.workflow_actions = workflow_actions
        self.triggers = triggers
        self.trigger_runs = trigger_runs
        self.trigger_service = trigger_service
        self.events = events
        self.sync_queue = sync_queue
        self.local_runtime = local_runtime
        self.local_model_provider = local_model_provider
        self.computer_runtime = computer_runtime
        self.browser_runtime = browser_runtime
        self.print_runtime = print_runtime
        self.connector_runtime = connector_runtime
        self.attachment_provider = attachment_provider
        self.database = database
        self.local_database = local_database
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
        """Whether this platform is backed by durable persistence.

        True for the cloud Postgres runtime and for the local runtime when it
        owns a durable SQLite store; false only for a purely in-memory build.
        """
        return self.database is not None or self.local_database is not None

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
            attachment_provider=self.attachment_provider,
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
        if self.local_database is not None:
            await self.local_database.dispose()

    async def initialize(self) -> None:
        """Load the local model and publish its status to the runtime.

        Model construction is synchronous and cheap; *loading* the weights is
        not, so it happens here, once, at process start. When no local model is
        configured this is a no-op and the platform behaves exactly as before.

        The load is non-fatal: if the model cannot be loaded, the provider is
        marked unavailable and routing fails closed for any request that
        requires a local model, rather than silently using the cloud.
        """
        provider = self.local_model_provider
        if provider is None:
            return
        try:
            await provider.load()
            healthy = await provider.probe()
        except Exception as exc:  # startup must not crash on a bad model
            healthy = False
            detail = f"local model load failed: {exc}"
            logger.warning(detail)
        else:
            detail = "local model loaded and healthy" if healthy else "local model not healthy"
        if self.local_runtime is not None:
            self.local_runtime.set_local_model_status(available=healthy, detail=detail)
        if not healthy:
            logger.warning("local model unavailable: %s", detail)

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
        workflow_store: WorkflowStore | None = None,
        workflow_version_store: WorkflowVersionStore | None = None,
        workflow_run_store: WorkflowRunStore | None = None,
        workflow_actions: ActionRegistry | None = None,
        trigger_store: TriggerStore | None = None,
        trigger_run_store: TriggerRunStore | None = None,
        event_bus: EventBus | None = None,
        sync_queue: SyncQueue | None = None,
        device_registry: DeviceRegistry | None = None,
        local_runtime: LocalRuntime | None = None,
        computer_runtime: ComputerRuntime | None = None,
        browser_runtime: BrowserRuntime | None = None,
        print_runtime: PrintRuntime | None = None,
        attachment_provider: AttachmentProvider | None = None,
        database: Database | None = None,
        local_database: LocalDatabase | None = None,
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
        is_local = settings.runtime_kind.value == "local"
        runtime = RuntimeProfile.local() if is_local else RuntimeProfile.cloud()

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
        local_model_provider: LocalBackendProvider | None = None
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
            remote_candidates: list[str] = []
            local_candidates: list[str] = []
            if settings.openai_api_key.get_secret_value():
                registry.register(
                    OpenAICompatibleProvider(
                        name="openai",
                        base_url=settings.openai_base_url,
                        api_key=settings.openai_api_key.get_secret_value(),
                        timeout=settings.request_timeout_seconds,
                    )
                )
                model = (
                    settings.default_model
                    if settings.default_model.startswith("openai/")
                    else "openai/gpt-4o-mini"
                )
                remote_candidates.append(model)
            # Anthropic is registered only when its key is configured. It speaks
            # the Messages API, so it needs its own adapter, but it is a remote
            # candidate like any other and the agent core never sees the
            # difference.
            if settings.anthropic_api_key.get_secret_value():
                registry.register(
                    AnthropicCompatibleProvider(
                        name="anthropic",
                        base_url=settings.anthropic_base_url,
                        api_key=settings.anthropic_api_key.get_secret_value(),
                        timeout=settings.request_timeout_seconds,
                    )
                )
                remote_candidates.append(f"anthropic/{settings.anthropic_model}")
            # A configured local endpoint (Ollama / llama.cpp server) runs on
            # this machine even though it speaks the OpenAI wire protocol.
            if settings.local_model_base_url:
                registry.register(
                    OpenAICompatibleLocalProvider(
                        name="local",
                        base_url=settings.local_model_base_url,
                        model=settings.local_model_name,
                        timeout=settings.request_timeout_seconds,
                    )
                )
                local_candidates.append(f"local/{settings.local_model_name}")
            # A file-backed local model (llama.cpp) is registered as a
            # first-class local provider. It is loaded asynchronously by
            # Platform.initialize() so constructing the platform never blocks
            # on a multi-hundred-megabyte file.
            if settings.local_model_path and settings.local_model_storage_dir:
                storage = resolve_storage_dir(settings.local_model_storage_dir)
                model_path = ensure_within_storage(
                    Path(settings.local_model_path), storage_dir=storage
                )
                descriptor = LocalModelDescriptor(
                    name="local",
                    backend=settings.local_model_backend,
                    model_path=str(model_path),
                    model_id=model_path.name,
                    context_window=settings.local_model_context_window,
                    capabilities=frozenset(
                        {ModelCapability.CHAT, ModelCapability.TOOL_CALLING, ModelCapability.CODE}
                    ),
                    sha256=settings.local_model_sha256 or None,
                )
                backend = build_backend(
                    descriptor,
                    n_threads=settings.local_model_n_threads or None,
                    n_gpu_layers=settings.local_model_n_gpu_layers,
                    timeout_seconds=settings.local_model_timeout_seconds,
                    isolate=settings.local_model_isolate,
                )
                local_model_provider = LocalBackendProvider(
                    descriptor=descriptor,
                    backend=backend,
                    timeout=settings.local_model_timeout_seconds,
                )
                registry.register(local_model_provider)
                local_candidates.append(model_logical_id(descriptor))
            candidates = [*local_candidates, *remote_candidates]
            setup_only = not candidates

            if setup_only:
                # First run of a fresh install: nothing is configured yet. Rather
                # than refuse to start — which would make it impossible to open
                # the product and configure a model — register an honest
                # setup-required provider. It performs no inference and every
                # call raises an actionable ConfigurationError, so a task can
                # never silently succeed against it. The desktop surfaces it as
                # ``needs_setup``; installing/activating a model replaces it.
                registry.register(SetupRequiredProvider())
                candidates = [SETUP_MODEL_ID]
            router = ModelRouter(
                # The setup placeholder must be reachable even in OFFLINE mode so
                # a task gets its actionable ConfigurationError instead of a
                # generic fail-closed refusal; it runs on this machine.
                local_models=(SETUP_MODEL_ID,) if setup_only else tuple(local_candidates),
                remote_models=tuple(remote_candidates),
            )
            routing_policy = RoutingPolicy(
                mode=settings.local_mode if is_local else OperatingMode.ONLINE,
                allow_remote=not is_local,
                prefer_local=settings.model_prefer_local,
                allow_remote_fallback=settings.model_allow_remote_fallback,
                local_only=is_local and settings.local_mode is OperatingMode.OFFLINE,
            )
            gateway = ModelGateway(
                registry,
                settings=settings,
                default_model=candidates[0],
                fallbacks=tuple(candidates[1:]),
                metrics=metrics,
                tracer=tracer,
                router=router,
                routing_policy=routing_policy,
            )
        logger.info("model gateway ready: default=%s", gateway.default_model)

        # --- Persistence selection -------------------------------------
        if database is None and settings.database_url.get_secret_value():
            database = Database(
                settings.database_url.get_secret_value(),
                pool_size=settings.db_pool_size,
                max_overflow=settings.db_max_overflow,
            )
        # A local runtime with no Postgres configured gets a durable embedded
        # SQLite store, so local state survives a restart. When a Postgres
        # database *is* configured it takes precedence (hybrid mode).
        if is_local and database is None:
            if local_database is None:
                local_database = LocalDatabase(default_store_path(settings.local_store_dir))
            local_database.upgrade()
        logger.info(
            "durable persistence: %s",
            "postgres"
            if database is not None
            else ("sqlite (local)" if local_database is not None else "in-memory"),
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
        # The local filesystem/command tools are registered only for a local
        # runtime. On a cloud runtime they would expose the *server's* disk, so
        # they must not exist there at all (defence in depth: policy would deny
        # them anyway, but absent tools cannot be invoked).
        local_tools = [tool_cls() for tool_cls in LOCAL_TOOLS] if is_local else []
        # Phase 6: workspace filesystem intelligence, document ingestion and
        # project understanding. These are workspace-sandboxed (not host-level),
        # so they are available in every runtime.
        document_tools = [tool_cls() for tool_cls in DOCUMENT_TOOLS]
        # Document generation (write-side): real PDF/DOCX/XLSX/CSV/Markdown/HTML/
        # JSON output. Workspace-sandboxed and approval-gated like any write, so
        # it is available in every runtime where a workspace exists.
        document_generation_tools = [tool_cls() for tool_cls in DOCUMENT_GENERATION_TOOLS]
        extended_filesystem_tools = [tool_cls() for tool_cls in EXTENDED_FILESYSTEM_TOOLS]
        project_tools = [tool_cls() for tool_cls in PROJECT_TOOLS]
        # Phase 7: computer use / perception. Local-only and opt-in — on a cloud
        # build, or when computer control is disabled, the tools do not exist, so
        # the model cannot even name them. Every action is still permission- and
        # approval-gated once they do exist.
        computer_tools = (
            [tool_cls() for tool_cls in COMPUTER_TOOLS]
            if is_local and settings.computer_control_enabled
            else []
        )
        # Phase 8: autonomous browsing and web research. Local-only and opt-in —
        # on a cloud build, or when browsing is disabled, the tools do not exist,
        # so the model cannot even name them. Every action is still permission-
        # and approval-gated once they do exist.
        browser_tools = (
            [tool_cls() for tool_cls in BROWSER_TOOLS]
            if is_local and settings.browser_enabled
            else []
        )
        # Phase 11: printing. Local-only and opt-in — printing is a physical
        # side effect. On a cloud build, or when printing is disabled, the tools
        # do not exist, so the model cannot even name them. Every job is still
        # permission- and approval-gated once they do exist.
        print_tools = (
            [tool_cls() for tool_cls in PRINT_TOOLS]
            if is_local and settings.printing_enabled
            else []
        )
        # Phase 9: external connectors and real-world actions. Opt-in. On a
        # cloud build the tools still exist (the cloud reaches SaaS APIs), but
        # only when connectors are enabled; every write/send is approval-gated.
        connector_tools = (
            [tool_cls() for tool_cls in CONNECTOR_TOOLS] if settings.connectors_enabled else []
        )
        tools = ToolRegistry(
            [
                EchoTool(),
                FinishTool(),
                ReadFileTool(),
                WriteFileTool(),
                ListDirectoryTool(),
                ShellCommandTool(),
                *extended_filesystem_tools,
                *document_tools,
                *document_generation_tools,
                *project_tools,
                *local_tools,
                *computer_tools,
                *browser_tools,
                *print_tools,
                *connector_tools,
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
            workflows: WorkflowStore = workflow_store or PostgresWorkflowStore(database)
            workflow_versions: WorkflowVersionStore = (
                workflow_version_store or PostgresWorkflowVersionStore(database)
            )
            workflow_runs: WorkflowRunStore = workflow_run_store or PostgresWorkflowRunStore(
                database
            )
            triggers: TriggerStore = trigger_store or PostgresTriggerStore(database)
            trigger_runs: TriggerRunStore = trigger_run_store or PostgresTriggerRunStore(database)
        elif local_database is not None:
            # Local durable store: the workflow, approval, audit, run and
            # external-action state the offline/desktop runtime needs is
            # persisted in SQLite, so it survives an application or runtime
            # restart. Memory, triggers and the task plan cache stay in-process
            # locally — the cloud keeps those, and the local runtime
            # deliberately persists only its working subset.
            approvals = approval_store or LocalApprovalStore(local_database)
            memory = memory_store or InMemoryMemoryStore()
            runs = run_store or LocalRunStore(local_database)
            selected_task_store = task_store or InMemoryTaskStore()
            workflows = workflow_store or LocalWorkflowStore(local_database)
            workflow_versions = workflow_version_store or LocalWorkflowVersionStore(local_database)
            workflow_runs = workflow_run_store or LocalWorkflowRunStore(local_database)
            triggers = trigger_store or InMemoryTriggerStore()
            trigger_runs = trigger_run_store or InMemoryTriggerRunStore()
        else:
            approvals = approval_store or InMemoryApprovalStore()
            memory = memory_store or InMemoryMemoryStore()
            runs = run_store or InMemoryRunStore()
            selected_task_store = task_store or InMemoryTaskStore()
            workflows = workflow_store or InMemoryWorkflowStore()
            workflow_versions = workflow_version_store or InMemoryWorkflowVersionStore()
            workflow_runs = workflow_run_store or InMemoryWorkflowRunStore()
            triggers = trigger_store or InMemoryTriggerStore()
            trigger_runs = trigger_run_store or InMemoryTriggerRunStore()

        if audit_log is not None:
            audit = audit_log
        elif database is not None:
            # Durable audit plus a structured log line for streaming/SIEM.
            audit = AuditLog([PostgresAuditStore(database), LoggingAuditSink()])
        elif local_database is not None:
            audit = AuditLog([LocalAuditSink(local_database), LoggingAuditSink()])
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
            idempotency = (
                LocalIdempotencyGuard(local_database)
                if local_database is not None
                else InMemoryIdempotencyGuard()
            )
            job_queue = queue or InMemoryJobQueue()

        # --- Local runtime ---------------------------------------------
        # Assembled only for a local runtime. It carries the filesystem sandbox
        # policy and the command policy, which are injected into every tool
        # context by the executor so local tools resolve paths through the
        # sandbox without the agent runner or workflow engine knowing about it.
        sync_queue_store: SyncQueue = sync_queue or (
            LocalSyncQueue(local_database) if local_database is not None else InMemorySyncQueue()
        )
        tool_metadata: dict[str, object] = {}
        if is_local:
            local_workspace = str((Path(settings.local_store_dir) / "workspace").resolve())
            Path(local_workspace).mkdir(parents=True, exist_ok=True)
            boundary = DataBoundaryPolicy(
                settings.local_mode,
                allow_confidential_remote=settings.local_allow_confidential_remote,
            )
            devices = device_registry or InMemoryDeviceRegistry()
            sync_coordinator = SyncCoordinator(sync_queue_store, boundary=boundary)
            if local_runtime is None:
                device = None
                if settings.local_control_enabled or settings.runtime_kind.value == "local":
                    device = enroll_device(
                        tenant_id="local",
                        user_id="local",
                        label=settings.local_device_label,
                        platform=detect_platform(),
                        instance_fingerprint=secrets.token_hex(16),
                    )
                local_runtime = build_local_runtime(
                    settings=settings,
                    audit=audit,
                    devices=devices,
                    sync=sync_coordinator,
                    workspace_dir=local_workspace,
                    allowed_roots=settings.local_workspace_roots or (local_workspace,),
                    denied_roots=settings.local_denied_roots,
                    device=device,
                    mode=settings.local_mode,
                    network_policy=settings.local_network_policy,
                )
            from .local.command import CommandPolicy
            from .local.sandbox import FilesystemPolicy

            tool_metadata["filesystem_policy"] = FilesystemPolicy(
                allowed_roots=local_runtime.sandbox.policy.allowed_roots,
                denied_roots=local_runtime.sandbox.policy.denied_roots,
                workspace_dir=local_workspace,
            )
            tool_metadata["command_policy"] = CommandPolicy(
                allowlist=frozenset(settings.local_command_allowlist),
                allowed_cwd_roots=local_runtime.sandbox.policy.allowed_roots,
            )
            # The local data boundary governs what may leave the machine; the
            # connector runtime consults it before any external transmission.
            tool_metadata["data_boundary"] = boundary

            # --- Computer use / perception (Phase 7) -------------------
            # Only assembled for a local build with computer control enabled.
            # The runtime carries the policy, budget, observation store and
            # screen-data boundary; it is injected into every tool context by the
            # executor, exactly like the filesystem/command policies.
            if computer_runtime is None and settings.computer_control_enabled:
                computer_backend = build_computer_backend()
                if settings.computer_backend not in ("auto", computer_backend.name):
                    computer_backend = build_computer_backend(platform=detect_platform())
                perception = PerceptionLayer(
                    computer_backend,
                    ocr=build_ocr_engine(languages=settings.computer_ocr_languages),
                    max_dimension=settings.computer_max_image_dimension,
                )
                try:
                    screen_policy = ScreenDataPolicy(settings.computer_screen_policy)
                except ValueError:
                    screen_policy = ScreenDataPolicy.LOCAL_ONLY
                screen_boundary = ScreenDataBoundary(
                    policy=screen_policy,
                    mode=settings.local_mode,
                    allow_confidential_remote=settings.local_allow_confidential_remote,
                )
                computer_policy = ComputerPolicy(
                    require_fresh_observation=settings.computer_require_fresh_observation,
                    max_observation_age_seconds=settings.computer_max_observation_age_seconds,
                    max_pointer_actions=settings.computer_max_pointer_actions,
                    max_keyboard_actions=settings.computer_max_keyboard_actions,
                    max_captures=settings.computer_max_captures,
                )
                computer_runtime = ComputerRuntime(
                    backend=computer_backend,
                    perception=perception,
                    policy=computer_policy,
                    audit=audit,
                    boundary=screen_boundary,
                )
                logger.info(
                    "computer backend: %s (available=%s)",
                    computer_backend.name,
                    computer_backend.available,
                )
            if computer_runtime is not None:
                tool_metadata["computer_runtime"] = computer_runtime
                # Offer the latest observation to a vision-capable model, subject
                # to the screen-data boundary. The destination is remote unless
                # the default model runs locally.
                attachment_provider = attachment_provider or ObservationAttachmentProvider(
                    runtime=computer_runtime,
                    boundary=computer_runtime.boundary,
                    destination_is_remote=not gateway.default_is_local(),
                )

            # --- Autonomous browsing / web research (Phase 8) ----------
            # Only assembled for a local build with browsing enabled. The runtime
            # carries the URL/domain policy, action budget, observation store,
            # web-content boundary and source ledger; it is injected into every
            # tool context by the executor, exactly like the computer runtime.
            if browser_runtime is None and settings.browser_enabled:
                browser_download_dir = str(
                    (Path(settings.local_store_dir) / "browser_downloads").resolve()
                )
                browser_backend = build_browser_backend(
                    backend=settings.browser_backend,
                    platform=detect_platform(),
                    headless=settings.browser_headless,
                    executable_path=settings.browser_executable_path,
                    download_dir=browser_download_dir,
                    max_download_bytes=settings.browser_max_download_bytes,
                    timeout_ms=settings.browser_timeout_ms,
                )
                try:
                    content_policy = WebContentPolicy(settings.browser_content_policy)
                except ValueError:
                    content_policy = WebContentPolicy.LOCAL_ONLY
                web_boundary = WebDataBoundary(
                    policy=content_policy,
                    mode=settings.local_mode,
                    allow_confidential_remote=settings.local_allow_confidential_remote,
                )
                url_policy = UrlPolicy(
                    allowed_domains=settings.browser_allowed_domains,
                    blocked_domains=settings.browser_blocked_domains,
                    allow_local_addresses=settings.browser_allow_local_addresses,
                    allow_file_urls=settings.browser_allow_file_urls,
                    allowed_file_roots=settings.browser_allowed_file_roots or (local_workspace,),
                )
                browser_policy = BrowserPolicy(
                    url_policy=url_policy,
                    max_navigations=settings.browser_max_navigations,
                    max_interactions=settings.browser_max_interactions,
                    max_observations=settings.browser_max_observations,
                    max_downloads=10,
                    max_uploads=10,
                    max_download_bytes=settings.browser_max_download_bytes,
                    max_tabs=settings.browser_max_tabs,
                    require_fresh_observation=settings.browser_require_fresh_observation,
                    max_observation_age_seconds=settings.browser_max_observation_age_seconds,
                    default_timeout_ms=settings.browser_timeout_ms,
                    require_approval_for_submit=settings.browser_require_approval_for_submit,
                )
                browser_runtime = BrowserRuntime(
                    backend=browser_backend,
                    policy=browser_policy,
                    audit=audit,
                    boundary=web_boundary,
                )
                logger.info(
                    "browser backend: %s (available=%s, reason=%s)",
                    browser_backend.name,
                    browser_backend.available,
                    browser_backend.unavailable_reason or "-",
                )
            if browser_runtime is not None:
                tool_metadata["browser_runtime"] = browser_runtime
                # Prefer the computer screenshot provider when both are enabled
                # (they share the same vision path); otherwise offer the browser
                # screenshot, subject to the web-content boundary.
                if attachment_provider is None:
                    attachment_provider = BrowserAttachmentProvider(
                        runtime=browser_runtime,
                        boundary=browser_runtime.boundary,
                        destination_is_remote=not gateway.default_is_local(),
                    )

        # --- Printing (Phase 11) ---------------------------------------
        # Real spooler integration (CUPS on POSIX, the Win32 spooler on
        # Windows). Local-only and opt-in: printing is a physical side effect.
        # The runtime enforces policy, a per-run job budget, the data boundary
        # and audit; it is injected into every tool context, exactly like the
        # computer and browser runtimes.
        if is_local and print_runtime is None and settings.printing_enabled:
            print_backend = build_print_backend(platform=detect_platform())
            print_policy = PrintPolicy(
                max_document_bytes=settings.print_max_document_bytes,
                max_jobs_per_run=settings.print_max_jobs_per_run,
                min_job_interval_seconds=settings.print_min_job_interval_seconds,
            )
            # Job metadata is durable whenever a local SQLite store exists, so a
            # submitted job can be reviewed, reconciled or cancelled after a
            # restart. A purely in-memory build (tests without a store) falls
            # back to the in-memory store.
            print_store: PrintJobStore = (
                LocalPrintJobStore(local_database)
                if local_database is not None
                else InMemoryPrintJobStore()
            )
            print_runtime = PrintRuntime(
                backend=print_backend,
                policy=print_policy,
                audit=audit,
                boundary=boundary,
                store=print_store,
            )
            logger.info(
                "print backend: %s (available=%s, reason=%s)",
                print_backend.name,
                print_backend.available,
                print_backend.unavailable_reason or "-",
            )
        if print_runtime is not None:
            tool_metadata["print_runtime"] = print_runtime

        # --- External connectors (Phase 9) -----------------------------
        # Built from declarative configuration. The runtime enforces the data
        # boundary, approval binding, the idempotency ledger and verification;
        # it is injected into every tool context, exactly like the browser and
        # computer runtimes. The ledger is local for now (a Postgres-backed
        # ledger is the cloud deployment's swap, behind the same interface).
        connector_runtime: ConnectorRuntime | None = None
        if connector_tools:
            from .local.sandbox import FilesystemPolicy

            connector_credentials = CredentialResolver()
            connector_fs = tool_metadata.get("filesystem_policy")
            connector_registry = build_connectors(
                parse_connector_specs(settings.connectors_json),
                credentials=connector_credentials,
                filesystem_policy=(
                    connector_fs if isinstance(connector_fs, FilesystemPolicy) else None
                ),
                workspace_dir=str(Path(settings.local_store_dir) / "workspace"),
                allow_local_addresses=settings.connectors_allow_local_addresses,
                max_response_bytes=settings.connector_max_response_bytes,
                retry_max_attempts=settings.connector_retry_max_attempts,
            )
            connector_boundary = tool_metadata.get("data_boundary")
            # The ledger is durable whenever a local SQLite store exists, so a
            # consequential external effect is not repeated after a restart. A
            # purely in-memory build (tests without a store) falls back to the
            # in-memory ledger.
            action_ledger = (
                LocalActionLedger(local_database)
                if local_database is not None
                else InMemoryActionLedger()
            )
            connector_runtime = ConnectorRuntime(
                registry=connector_registry,
                audit=audit,
                ledger=action_ledger,
                boundary=(
                    connector_boundary
                    if isinstance(connector_boundary, DataBoundaryPolicy)
                    else None
                ),
            )
            tool_metadata["connector_runtime"] = connector_runtime

        executor = ToolExecutor(
            registry=tools,
            policy=policy,
            approvals=approvals,
            audit=audit,
            approval_ttl_seconds=settings.approval_ttl_seconds,
            idempotency=idempotency,
            metrics=metrics,
            tracer=tracer,
            tool_metadata=tool_metadata,
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

        # --- Workflow engine -------------------------------------------
        actions = workflow_actions or default_actions()
        workflow_engine = WorkflowEngine(
            platform=None,  # patched below; the engine needs the built platform
            runs=workflow_runs,
            versions=workflow_versions,
            actions=actions,
        )

        # --- Triggers ---------------------------------------------------
        events = event_bus or EventBus()
        trigger_service = TriggerService(None, settings=settings)

        platform = cls(
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
            workflows=workflows,
            workflow_versions=workflow_versions,
            workflow_runs=workflow_runs,
            workflow_engine=workflow_engine,
            workflow_actions=actions,
            triggers=triggers,
            trigger_runs=trigger_runs,
            trigger_service=trigger_service,
            events=events,
            sync_queue=sync_queue_store,
            local_runtime=local_runtime,
            local_model_provider=local_model_provider,
            computer_runtime=computer_runtime,
            browser_runtime=browser_runtime,
            print_runtime=print_runtime,
            connector_runtime=connector_runtime,
            attachment_provider=attachment_provider,
            database=database,
            local_database=local_database,
            redis_client=redis_client,
            metrics=metrics,
            tracer=tracer,
            readiness=readiness,
        )
        # The engine holds a reference to its platform (it reuses the runner,
        # executor, policy engine and audit log). Break the construction cycle
        # by binding it once the platform exists.
        workflow_engine.bind(platform)
        trigger_service.bind(platform)
        return platform

    def memory_policy_for(self, tenant_id: str) -> MemoryPolicy:
        """Per-tenant memory policy (defaults, extensible per tenant)."""
        return MemoryPolicy.default(tenant_id=tenant_id)
