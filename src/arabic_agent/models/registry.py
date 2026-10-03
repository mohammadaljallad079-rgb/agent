"""Provider registry.

Maps logical model identifiers of the form ``"<provider>/<model>"`` to a
provider instance. This is the seam that lets operators add a new cloud or
local model without modifying the agent core.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..core.errors import ConfigurationError, NotFoundError
from ..core.ids import new_id
from .base import ModelProvider


class ProviderRegistry:
    """A mutable registry of model providers keyed by provider name."""

    def __init__(self) -> None:
        self._providers: dict[str, ModelProvider] = {}

    def register(self, provider: ModelProvider, *, replace: bool = False) -> None:
        if provider.name in self._providers and not replace:
            raise ConfigurationError(f"provider already registered: {provider.name}")
        self._providers[provider.name] = provider

    def unregister(self, name: str) -> None:
        self._providers.pop(name, None)

    def get(self, name: str) -> ModelProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise NotFoundError(
                f"unknown model provider: {name}",
                details={"known": sorted(self._providers)},
            ) from exc

    def has(self, name: str) -> bool:
        return name in self._providers

    def names(self) -> list[str]:
        return sorted(self._providers)

    def all(self) -> Iterable[ModelProvider]:
        return tuple(self._providers.values())

    @staticmethod
    def split_model_id(model_id: str) -> tuple[str, str]:
        """Split ``"openai/gpt-4o-mini"`` into ``("openai", "gpt-4o-mini")``.

        A bare model name without a provider prefix is rejected: implicit
        routing is a common source of production incidents.
        """
        if "/" not in model_id:
            raise ConfigurationError(f"model id must be '<provider>/<model>', got {model_id!r}")
        provider, _, model = model_id.partition("/")
        if not provider or not model:
            raise ConfigurationError(f"malformed model id: {model_id!r}")
        return provider, model

    def resolve(self, model_id: str) -> tuple[ModelProvider, str]:
        """Return ``(provider, bare_model_name)`` for a logical model id."""
        provider_name, model = self.split_model_id(model_id)
        return self.get(provider_name), model

    def close_all(self) -> None:
        """Best-effort synchronous close hook (async close done by gateway)."""
        self._providers.clear()

    @staticmethod
    def new_provider_name(prefix: str = "prov") -> str:
        return new_id(prefix)
