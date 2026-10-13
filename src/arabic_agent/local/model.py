"""Local model providers.

The agent core already talks to *any* model through
:class:`~arabic_agent.models.base.ModelProvider` and the
:class:`~arabic_agent.models.gateway.ModelGateway`. A local model is therefore
not a new concept in the core — it is another provider registered under a name
like ``local``.

This module adds two things that are specific to the *local* runtime:

* :class:`LocalModelProvider` — an explicit marker interface for a provider that
  runs on the user's machine, so the runtime can ask "is a local model
  available?" without knowing which engine backs it.
* :func:`probe_local_model` — a **real** availability probe against an
  OpenAI-compatible local endpoint (Ollama, llama.cpp, vLLM). It performs an
  actual HTTP request; it never claims availability it did not observe.

Supported engines are *not* hardcoded. Anything exposing the OpenAI chat
schema (``GET /models``) works, which covers the engines listed in the roadmap.
No fake local model is provided: if nothing is reachable, the probe reports
unavailable and the runtime says so.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from ..core.enums import ModelCapability
from ..core.errors import ConfigurationError
from ..models.base import ModelProvider, ModelRequest, ModelResponse, ModelStreamDelta
from ..models.providers.openai_compatible import OpenAICompatibleProvider
from .inference import LlamaCppBackend, LocalInferenceBackend, OllamaBackend
from .model_registry import LocalModelDescriptor
from .model_security import verify_model_file
from .subprocess_backend import build_isolated_backend


class LocalModelProvider(ModelProvider):
    """A model provider that runs locally on the user's machine.

    Concrete local engines are ordinary :class:`ModelProvider` implementations;
    this subclass exists so the runtime can distinguish a local provider from a
    cloud one (for boundary and routing decisions) without importing a specific
    engine.
    """

    #: Whether the engine is reachable right now (set by a real probe).
    available: bool = False
    #: This provider runs on the user's machine.
    is_local: bool = True

    @abstractmethod
    async def probe(self) -> bool:
        """Perform a real reachability check; return availability."""


class OpenAICompatibleLocalProvider(OpenAICompatibleProvider, LocalModelProvider):
    """A local engine that speaks the OpenAI chat schema.

    Works with Ollama (``http://localhost:11434/v1``), llama.cpp server
    (``http://localhost:8080/v1``) and vLLM, all of which expose ``/models``.
    """

    def __init__(
        self,
        *,
        name: str = "local",
        base_url: str,
        model: str,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url:
            raise ConfigurationError("a local model provider requires a base_url")
        super().__init__(
            name=name,
            base_url=base_url,
            api_key=None,
            timeout=timeout,
            client=client,
        )
        self.model = model
        self.capabilities = frozenset(
            {ModelCapability.CHAT, ModelCapability.TOOL_CALLING, ModelCapability.CODE}
        )

    async def probe(self) -> bool:
        """Return whether the local endpoint answers ``GET /models``."""
        client = await self._get_client()
        try:
            response = await client.get(
                f"{self._base_url}/models", headers=self._headers(), timeout=5.0
            )
        except httpx.HTTPError:
            self.available = False
            return False
        self.available = response.status_code == 200
        return self.available


class LocalModelStatus:
    """A plain, serialisable report of local-model availability."""

    __slots__ = ("available", "base_url", "detail", "model", "provider")

    def __init__(
        self,
        *,
        available: bool,
        provider: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        detail: str = "",
    ) -> None:
        self.available = available
        self.provider = provider
        self.model = model
        self.base_url = base_url
        self.detail = detail

    def as_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "detail": self.detail,
        }


async def probe_local_model(
    *,
    base_url: str,
    model: str,
    provider_name: str = "local",
) -> tuple[LocalModelProvider | None, LocalModelStatus]:
    """Probe a local endpoint and return ``(provider, status)``.

    Returns ``(None, status)`` when no endpoint is configured or it is not
    reachable — the caller must not register an unavailable provider.
    """
    if not base_url:
        return None, LocalModelStatus(available=False, detail="no local model endpoint configured")
    provider = OpenAICompatibleLocalProvider(name=provider_name, base_url=base_url, model=model)
    try:
        reachable = await provider.probe()
    except Exception as exc:  # pragma: no cover - defensive
        await provider.aclose()
        return None, LocalModelStatus(
            available=False, base_url=base_url, model=model, detail=f"probe failed: {exc}"
        )
    if not reachable:
        await provider.aclose()
        return None, LocalModelStatus(
            available=False,
            provider=provider_name,
            base_url=base_url,
            model=model,
            detail="endpoint did not answer GET /models",
        )
    return provider, LocalModelStatus(
        available=True,
        provider=provider_name,
        base_url=base_url,
        model=model,
        detail="local endpoint reachable",
    )


class LocalBackendProvider(LocalModelProvider):
    """A :class:`LocalModelProvider` backed by a real local inference engine.

    This is the first-class local provider: it wraps a
    :class:`~arabic_agent.local.inference.LocalInferenceBackend` (llama.cpp or
    Ollama) behind the ordinary :class:`~arabic_agent.models.base.ModelProvider`
    contract, so the agent loop, tool executor, permission layer, approvals and
    audit are all unchanged. It advertises the capabilities declared on its
    descriptor — never guessed.
    """

    def __init__(
        self,
        *,
        descriptor: LocalModelDescriptor,
        backend: LocalInferenceBackend,
        timeout: float = 120.0,
    ) -> None:
        self.name = descriptor.name
        self._descriptor = descriptor
        self._backend = backend
        self._timeout = timeout
        self.capabilities = frozenset(descriptor.capabilities)
        self.is_local = True
        # A local model has a hard, known token window; the context manager uses
        # it to size prompts so a small n_ctx is never overflowed at call time.
        self.context_window = int(descriptor.context_window) or None
        self.available = False

    @property
    def descriptor(self) -> LocalModelDescriptor:
        return self._descriptor

    async def load(self) -> None:
        await self._backend.load()
        self.available = True

    async def probe(self) -> bool:
        """Real readiness check: is the engine loaded and able to serve?"""
        try:
            self.available = await self._backend.healthy()
        except Exception:  # a probe must never raise
            self.available = False
        return self.available

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return await self._backend.generate(request, timeout_seconds=self._timeout)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamDelta]:
        async for delta in self._backend.stream(request, timeout_seconds=self._timeout):
            yield delta

    async def aclose(self) -> None:
        await self._backend.close()
        self.available = False

    def model_info(self) -> dict[str, Any]:
        """Backend-reported metadata; nothing here is invented."""
        return {**self._backend.model_info(), "capabilities": sorted(self.capabilities)}


def build_backend(
    descriptor: LocalModelDescriptor,
    *,
    model_path: str | None = None,
    n_threads: int | None = None,
    n_gpu_layers: int = 0,
    timeout_seconds: float = 120.0,
    isolate: bool = False,
) -> LocalInferenceBackend:
    """Construct the inference backend a descriptor names.

    File integrity is checked here (when a hash is configured) so a tampered or
    corrupted weights file is rejected before it is ever loaded.

    ``isolate`` runs a file-backed (llama.cpp) engine in a dedicated child
    process so a native crash cannot take the agent down. It has no effect on
    endpoint-backed engines, which are already external processes.
    """
    if descriptor.backend == "llama_cpp":
        path = model_path or descriptor.model_path
        if not path:
            raise ConfigurationError("a llama_cpp model requires a model_path")
        verify_model_file(Path(path), sha256=descriptor.sha256)
        if isolate:
            return build_isolated_backend(
                model_path=path,
                context_window=descriptor.context_window,
                n_threads=n_threads,
                n_gpu_layers=n_gpu_layers,
                sha256=descriptor.sha256 or None,
                timeout_seconds=timeout_seconds,
            )
        return LlamaCppBackend(
            model_path=path,
            context_window=descriptor.context_window,
            n_threads=n_threads,
            n_gpu_layers=n_gpu_layers,
        )
    if descriptor.backend == "ollama":
        if not descriptor.endpoint:
            raise ConfigurationError("an ollama model requires an endpoint")
        return OllamaBackend(
            model=descriptor.model_id, base_url=descriptor.endpoint, timeout=timeout_seconds
        )
    raise ConfigurationError(f"unsupported local backend: {descriptor.backend}")


async def discover_local_model(
    descriptor: LocalModelDescriptor,
    *,
    model_path: str | None = None,
    n_threads: int | None = None,
    n_gpu_layers: int = 0,
    timeout_seconds: float = 120.0,
    isolate: bool = False,
) -> tuple[LocalBackendProvider | None, LocalModelStatus]:
    """Discover and prepare a local model, returning ``(provider, status)``.

    For file-backed backends the model is loaded (real inference readiness); for
    endpoint-backed backends the server is probed. If anything fails, the
    provider is closed and ``None`` is returned — the caller must not register
    an unavailable provider.
    """
    try:
        backend = build_backend(
            descriptor,
            model_path=model_path,
            n_threads=n_threads,
            n_gpu_layers=n_gpu_layers,
            timeout_seconds=timeout_seconds,
            isolate=isolate,
        )
    except Exception as exc:  # report, do not crash startup
        return None, LocalModelStatus(
            available=False,
            provider=descriptor.name,
            model=descriptor.model_id,
            detail=f"backend not ready: {exc}",
        )
    provider = LocalBackendProvider(descriptor=descriptor, backend=backend, timeout=timeout_seconds)
    try:
        await provider.load()
        healthy = await provider.probe()
    except Exception as exc:
        await provider.aclose()
        return None, LocalModelStatus(
            available=False,
            provider=descriptor.name,
            model=descriptor.model_id,
            detail=f"model load failed: {exc}",
        )
    if not healthy:
        await provider.aclose()
        return None, LocalModelStatus(
            available=False,
            provider=descriptor.name,
            model=descriptor.model_id,
            detail="backend did not report healthy after load",
        )
    return provider, LocalModelStatus(
        available=True,
        provider=descriptor.name,
        model=descriptor.model_id,
        detail="local model loaded and healthy",
    )
