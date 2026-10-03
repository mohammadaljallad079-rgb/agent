"""Model Gateway.

Single entry point the agent uses to talk to *any* model. It owns routing,
transient-failure retries with backoff, fallback chains, and usage accounting.
The agent core never imports a concrete provider.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from ..core.config import Settings
from ..core.enums import ModelCapability
from ..core.errors import ExternalServiceError, PlatformError, TransientError
from .base import ModelProvider, ModelRequest, ModelResponse, ModelStreamDelta
from .registry import ProviderRegistry

logger = logging.getLogger(__name__)


@dataclass
class GatewayStats:
    """Aggregated usage across a gateway's lifetime."""

    requests: int = 0
    failures: int = 0
    retries: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
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
    ) -> None:
        self._registry = registry
        self._settings = settings
        self._default_model = default_model or settings.default_model
        self._fallbacks = tuple(fallbacks)
        self.stats = GatewayStats()
        # Validate the default model is routable at construction time so
        # misconfiguration fails fast instead of on the first user request.
        self._registry.resolve(self._default_model)

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

    async def complete(
        self,
        request: ModelRequest,
        *,
        capability: ModelCapability = ModelCapability.CHAT,
        allow_fallback: bool = True,
    ) -> ModelResponse:
        """Complete a request, retrying transient failures and falling back.

        Retries apply to :class:`TransientError` and ``retryable`` failures.
        Validation/permission errors propagate immediately — retrying them
        wastes budget and hides bugs.
        """
        models = (
            self._candidate_models(request.model)
            if allow_fallback
            else [request.model or self._default_model]
        )
        last_error: PlatformError | None = None

        for index, model_id in enumerate(models):
            provider, bare_model = self._resolve(model_id, capability)
            routed = request.model_copy(update={"model": bare_model})
            try:
                response = await self._complete_with_retry(provider, routed, model_id)
            except PlatformError as exc:
                last_error = exc
                if not exc.retryable and not isinstance(exc, TransientError):
                    # Non-retryable: don't try other models, surface immediately.
                    raise
                logger.warning("model %s failed: %s", model_id, exc.message)
                if index < len(models) - 1:
                    continue
                raise
            self.stats.record(
                model_id, response.usage.prompt_tokens, response.usage.completion_tokens
            )
            if model_id != (request.model or self._default_model):
                logger.info("served request via fallback model %s", model_id)
            return response

        # Unreachable in practice, but keeps the type checker and runtime honest.
        raise last_error or ExternalServiceError("no model candidates available")

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
