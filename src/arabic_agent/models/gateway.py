"""Model Gateway.

Single entry point the agent uses to talk to *any* model. It owns routing,
transient-failure retries with backoff, fallback chains, and usage accounting.
The agent core never imports a concrete provider.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from ..core.config import Settings
from ..core.enums import DataSensitivity, ModelCapability
from ..core.errors import ExternalServiceError, PlatformError, TransientError
from ..observability.metrics import Metrics
from ..observability.tracing import Tracer, span
from .base import ModelProvider, ModelRequest, ModelResponse, ModelStreamDelta
from .registry import ProviderRegistry
from .routing import ModelRouter, RoutingDecision, RoutingPolicy, fail_closed_error

logger = logging.getLogger(__name__)


@dataclass
class GatewayStats:
    """Aggregated usage across a gateway's lifetime."""

    requests: int = 0
    failures: int = 0
    retries: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    local_requests: int = 0
    remote_requests: int = 0
    fallbacks: int = 0
    refusals: int = 0
    per_model: dict[str, int] = field(default_factory=dict)

    def record(self, model_id: str, prompt_tokens: int, completion_tokens: int) -> None:
        self.requests += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.per_model[model_id] = self.per_model.get(model_id, 0) + 1


class ModelGateway:
    """Routes completion requests to providers with retry and fallback."""

    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        settings: Settings,
        default_model: str | None = None,
        fallbacks: Sequence[str] = (),
        metrics: Metrics | None = None,
        tracer: Tracer | None = None,
        router: ModelRouter | None = None,
        routing_policy: RoutingPolicy | None = None,
    ) -> None:
        self._registry = registry
        self._settings = settings
        self._default_model = default_model or settings.default_model
        self._fallbacks = tuple(fallbacks)
        self.stats = GatewayStats()
        self._metrics = metrics
        self._tracer = tracer
        self._router = router
        self._routing_policy = routing_policy
        # Validate the default model is routable at construction time so
        # misconfiguration fails fast instead of on the first user request.
        self._registry.resolve(self._default_model)

    @property
    def router(self) -> ModelRouter | None:
        return self._router

    @property
    def routing_policy(self) -> RoutingPolicy | None:
        return self._routing_policy

    def set_routing_policy(self, policy: RoutingPolicy) -> None:
        """Replace the routing policy (e.g. when the runtime mode changes)."""
        self._routing_policy = policy

    @property
    def default_model(self) -> str:
        return self._default_model

    @property
    def fallbacks(self) -> tuple[str, ...]:
        return self._fallbacks

    def _resolve(self, model_id: str, capability: ModelCapability) -> tuple[ModelProvider, str]:
        provider, model = self._registry.resolve(model_id)
        if not provider.supports(capability):
            raise ExternalServiceError(
                f"model {model_id!r} does not support capability {capability.value!r}",
                details={"provider": provider.name},
            )
        return provider, model

    def _candidate_models(self, model_id: str | None) -> list[str]:
        primary = model_id or self._default_model
        seen = {primary}
        candidates = [primary]
        for fb in self._fallbacks:
            if fb not in seen:
                candidates.append(fb)
                seen.add(fb)
        return candidates

    def route(
        self,
        *,
        capability: ModelCapability = ModelCapability.CHAT,
        sensitivity: DataSensitivity = DataSensitivity.INTERNAL,
        requested_model: str | None = None,
    ) -> RoutingDecision | None:
        """Return the routing decision, or ``None`` when no router is configured.

        With no router the gateway preserves its original behaviour (primary
        then fallbacks), which keeps existing cloud deployments unchanged.
        """
        if self._router is None or self._routing_policy is None:
            return None
        return self._router.decide(
            policy=self._routing_policy,
            capability=capability,
            sensitivity=sensitivity,
            requested_model=requested_model,
        )

    async def complete(
        self,
        request: ModelRequest,
        *,
        capability: ModelCapability = ModelCapability.CHAT,
        allow_fallback: bool = True,
        sensitivity: DataSensitivity = DataSensitivity.INTERNAL,
    ) -> ModelResponse:
        """Complete a request, retrying transient failures and falling back.

        Retries apply to :class:`TransientError` and ``retryable`` failures.
        Validation/permission errors propagate immediately — retrying them
        wastes budget and hides bugs.

        When a router is configured, the candidate list is chosen by policy. A
        fail-closed decision (local required but unavailable) raises instead of
        falling back to a remote model — private data is never silently sent to
        the cloud.
        """
        decision = self.route(
            capability=capability,
            sensitivity=sensitivity,
            requested_model=request.model,
        )
        if decision is not None:
            if decision.fail_closed:
                self.stats.refusals += 1
                logger.warning("model routing refused request: %s", decision.reason)
                raise fail_closed_error(decision)
            models = list(decision.candidates)
            if not allow_fallback:
                models = models[:1]
        else:
            models = (
                self._candidate_models(request.model)
                if allow_fallback
                else [request.model or self._default_model]
            )
        last_error: PlatformError | None = None
        primary = models[0] if models else None

        for index, model_id in enumerate(models):
            provider, bare_model = self._resolve(model_id, capability)
            routed = request.model_copy(update={"model": bare_model})
            started = time.perf_counter()
            try:
                with span(
                    "model.complete",
                    attributes={"model": model_id, "capability": capability.value},
                    tracer=self._tracer,
                ):
                    response = await self._complete_with_retry(provider, routed, model_id)
            except PlatformError as exc:
                last_error = exc
                self._record_model_metric(model_id, "error", started)
                if not exc.retryable and not isinstance(exc, TransientError):
                    # Non-retryable: don't try other models, surface immediately.
                    raise
                logger.warning("model %s failed: %s", model_id, exc.message)
                if index < len(models) - 1:
                    if decision is not None:
                        self.stats.fallbacks += 1
                    continue
                raise
            self._record_model_metric(model_id, "ok", started)
            self.stats.record(
                model_id, response.usage.prompt_tokens, response.usage.completion_tokens
            )
            self._record_location(model_id)
            if model_id != primary:
                logger.info("served request via fallback model %s", model_id)
            return response

        # Unreachable in practice, but keeps the type checker and runtime honest.
        raise last_error or ExternalServiceError("no model candidates available")

    def _record_location(self, model_id: str) -> None:
        """Track local vs remote request counts for cost accounting."""
        provider_name = model_id.split("/", 1)[0]
        provider = self._registry.get(provider_name)
        if provider.is_local:
            self.stats.local_requests += 1
        else:
            self.stats.remote_requests += 1

    def _record_model_metric(self, model_id: str, outcome: str, started: float) -> None:
        if self._metrics is None:
            return
        self._metrics.model_call(
            model=model_id, outcome=outcome, duration_seconds=time.perf_counter() - started
        )

    async def _complete_with_retry(
        self, provider: ModelProvider, request: ModelRequest, model_id: str
    ) -> ModelResponse:
        attempts = self._settings.max_tool_retries + 1
        delay = 0.25
        last: PlatformError | None = None
        for attempt in range(attempts):
            try:
                return await provider.complete(request)
            except PlatformError as exc:
                last = exc
                if not exc.retryable:
                    raise
                self.stats.retries += 1
                if self._metrics is not None:
                    self._metrics.model_retry(model=model_id)
                if attempt < attempts - 1:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 4.0)
        assert last is not None
        self.stats.failures += 1
        raise last

    async def stream(
        self,
        request: ModelRequest,
        *,
        capability: ModelCapability = ModelCapability.CHAT,
    ) -> AsyncIterator[ModelStreamDelta]:
        """Stream from the primary model (no fallback mid-stream)."""
        model_id = request.model or self._default_model
        provider, bare_model = self._resolve(model_id, capability)
        routed = request.model_copy(update={"model": bare_model})
        async for delta in provider.stream(routed):
            yield delta

    async def aclose(self) -> None:
        for provider in self._registry.all():
            await provider.aclose()
