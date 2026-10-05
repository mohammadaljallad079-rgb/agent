"""Local model registry.

A *descriptor* is everything the platform knows about one local model without
loading it: which backend runs it, where it lives, its context window, and the
capabilities it can actually serve. Capabilities are **declared by
configuration or read from backend metadata** — this module never guesses a
capability from a model's name.

The registry is the single place a local model becomes addressable as
``"local/<model>"`` through the existing
:class:`~arabic_agent.models.registry.ProviderRegistry`.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..core.enums import DataSensitivity, ModelCapability
from ..core.errors import ConfigurationError, NotFoundError
from .hardware import HardwareProfile, ResourceRequirement, can_run

#: Backends the platform knows how to speak to. The abstraction is not tied to
#: any one of them: a new engine is added by registering another value here and
#: providing a matching :class:`LocalInferenceBackend` implementation.
KNOWN_BACKENDS = ("llama_cpp", "ollama", "openai_compatible")


class LocalModelDescriptor(BaseModel):
    """The declarative description of a locally-hosted model."""

    model_config = ConfigDict(frozen=True)

    #: Stable identifier used in routing config, e.g. ``"local"``.
    name: str
    #: Which engine runs it (one of :data:`KNOWN_BACKENDS`).
    backend: str
    #: For file-backed backends: the path to the weights (inside the store).
    model_path: str | None = None
    #: For endpoint-backed backends (ollama / openai_compatible): the base URL.
    endpoint: str | None = None
    #: The bare model name the backend expects (e.g. ``qwen2.5:7b``).
    model_id: str
    context_window: int = Field(default=4096, ge=1)
    capabilities: frozenset[ModelCapability] = frozenset({ModelCapability.CHAT})
    #: Approximate resources; used only for the runnable gate, never a promise.
    resources: ResourceRequirement = Field(default_factory=ResourceRequirement)
    #: Privacy classification of the model's *output/processing* location.
    privacy: DataSensitivity = DataSensitivity.INTERNAL
    #: Optional expected SHA-256 of the weights file (integrity check).
    sha256: str | None = None

    @field_validator("backend")
    @classmethod
    def _known_backend(cls, value: str) -> str:
        if value not in KNOWN_BACKENDS:
            raise ValueError(f"unknown local backend {value!r}; known: {', '.join(KNOWN_BACKENDS)}")
        return value

    @field_validator("capabilities", mode="before")
    @classmethod
    def _coerce_caps(cls, value: object) -> object:
        if isinstance(value, str):
            return frozenset(ModelCapability(c.strip()) for c in value.split(",") if c.strip())
        return value

    @property
    def requires_file(self) -> bool:
        return self.backend in ("llama_cpp",)

    @property
    def requires_endpoint(self) -> bool:
        return self.backend in ("ollama", "openai_compatible")

    def supports(self, capability: ModelCapability) -> bool:
        return capability in self.capabilities


def model_logical_id(descriptor: LocalModelDescriptor) -> str:
    """Return the ``"<provider>/<model>"`` id the gateway routes on."""
    return f"{descriptor.name}/{descriptor.model_id}"


class ModelRegistry:
    """A registry of local model descriptors, keyed by provider name."""

    def __init__(self) -> None:
        self._models: dict[str, LocalModelDescriptor] = {}

    def register(self, descriptor: LocalModelDescriptor, *, replace: bool = False) -> None:
        if descriptor.name in self._models and not replace:
            raise ConfigurationError(f"local model already registered: {descriptor.name}")
        self._models[descriptor.name] = descriptor

    def get(self, name: str) -> LocalModelDescriptor:
        try:
            return self._models[name]
        except KeyError as exc:
            raise NotFoundError(
                f"unknown local model: {name}", details={"known": sorted(self._models)}
            ) from exc

    def has(self, name: str) -> bool:
        return name in self._models

    def names(self) -> list[str]:
        return sorted(self._models)

    def all(self) -> tuple[LocalModelDescriptor, ...]:
        return tuple(self._models[name] for name in self.names())

    def logical_ids(self) -> list[str]:
        return [model_logical_id(d) for d in self.all()]


class LocalModelError(ConfigurationError):
    """Raised when a local model cannot be prepared (missing file, bad hash)."""


def resource_requirement_from_file(path: Path, *, context_window: int) -> ResourceRequirement:
    """Estimate load requirements from the real file size.

    A quantised model's resident size is roughly its file size plus the KV
    cache for the context window; the estimate is deliberately generous so the
    gate errs toward "not runnable" rather than claiming a machine can cope when
    it cannot.
    """
    size = path.stat().st_size if path.is_file() else 0
    # ~0.5 MiB per 1024 tokens of context is a safe upper bound across families.
    kv_cache = (context_window / 1024) * 512 * 1024
    return ResourceRequirement(
        min_ram_bytes=int(size * 1.2 + kv_cache),
        disk_bytes=size,
    )


def check_runnable(descriptor: LocalModelDescriptor, profile: HardwareProfile) -> tuple[bool, str]:
    """Convenience wrapper returning ``(runnable, reason)``."""
    verdict = can_run(profile, descriptor.resources)
    return verdict.runnable, verdict.reason
