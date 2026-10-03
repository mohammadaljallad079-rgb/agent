"""Composition root.

Wires the platform together from configuration. Nothing else in the codebase
constructs its own dependencies; the container is the single place where
concrete implementations are chosen, which keeps the agent core testable (tests
build a container with a deterministic model provider and a temp workspace).
"""

from __future__ import annotations

import logging

from .agent.executor import ToolExecutor
from .agent.runner import AgentRunner
from .audit.base import AuditLog
from .audit.logging_sink import LoggingAuditSink
from .audit.memory_sink import InMemoryAuditSink
from .context.manager import ContextManager
from .core.config import Settings
from .core.enums import Locale
from .core.errors import ConfigurationError
from .integrations.base import IntegrationRegistry
from .integrations.webhook import WebhookIntegration
from .memory.memory_store import InMemoryMemoryStore
from .memory.policy import MemoryPolicy
from .models.gateway import ModelGateway
from .models.providers.openai_compatible import OpenAICompatibleProvider
from .models.registry import ProviderRegistry
from .permissions.approvals import InMemoryApprovalStore
from .permissions.policy import PolicyEngine
from .runtime.boundary import RuntimeProfile
from .tasks.engine import TaskEngine
from .tasks.store import InMemoryTaskStore
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
        approvals: InMemoryApprovalStore,
        audit: AuditLog,
        memory: InMemoryMemoryStore,
        memory_policy: MemoryPolicy,
        tasks: TaskEngine,
        context: ContextManager,
        executor: ToolExecutor,
        integrations: IntegrationRegistry,
        runtime: RuntimeProfile,
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

    @classmethod
    def build(
        cls,
        settings: Settings | None = None,
        *,
        extra_tools: list[Tool] | None = None,
        provider_registry: ProviderRegistry | None = None,
        default_model: str | None = None,
        fallbacks: tuple[str, ...] = (),
        memory_store: InMemoryMemoryStore | None = None,
        task_store: InMemoryTaskStore | None = None,
        approval_store: InMemoryApprovalStore | None = None,
        audit_log: AuditLog | None = None,
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
            )
        logger.info("model gateway ready: default=%s", gateway.default_model)

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
        approvals = approval_store or InMemoryApprovalStore()
        audit = audit_log or AuditLog([InMemoryAuditSink(), LoggingAuditSink()])
        memory = memory_store or InMemoryMemoryStore()
        memory_policy = MemoryPolicy.default(tenant_id="__platform__")

        # --- Tasks & context -------------------------------------------
        tasks = TaskEngine(task_store or InMemoryTaskStore())
        context = ContextManager(settings)

        executor = ToolExecutor(
            registry=tools,
            policy=policy,
            approvals=approvals,
            audit=audit,
            approval_ttl_seconds=settings.approval_ttl_seconds,
        )

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
        )

    def memory_policy_for(self, tenant_id: str) -> MemoryPolicy:
        """Per-tenant memory policy (defaults, extensible per tenant)."""
        return MemoryPolicy.default(tenant_id=tenant_id)
