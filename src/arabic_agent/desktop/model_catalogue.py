"""Local model catalogue: the supported models the desktop offers.

The catalogue is *declarative data*, not a discovery service. Each entry names a
model, its backend, its approximate resource requirement and the capabilities it
can serve. Nothing here downloads anything or invents a capability from a model
name — a catalogue entry is a promise about a specific, operator-approved
artifact with a fixed source URL and, where known, an expected SHA-256.

Entries are deliberately conservative in their resource estimates so the
recommendation gate errs toward "this machine may not cope" rather than the
reverse. Adding a model is a data change; the manager code is unchanged.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import ModelCapability, ModelProfile
from ..local.hardware import ResourceRequirement

#: One gibibyte, for readable estimates.
_GIB = 1024**3


class CatalogueModel(BaseModel):
    """One supported local model, without loading it."""

    model_config = ConfigDict(frozen=True)

    #: Stable id used in requests and the installed manifest.
    id: str
    #: Human-facing name (English); the UI localises the surrounding chrome.
    display_name: str
    #: Optional Arabic display name.
    display_name_ar: str = ""
    profile: ModelProfile
    backend: str = "llama_cpp"
    #: Parameter count label (e.g. ``"3B"``), purely descriptive.
    parameters: str = ""
    quantization: str = ""
    #: Where the weights are fetched from (an operator-approved URL).
    url: str
    filename: str
    #: Expected SHA-256 of the weights file, when the catalogue knows it.
    sha256: str | None = None
    #: Expected size in bytes (checked after download when set).
    size_bytes: int = 0
    context_window: int = 4096
    capabilities: frozenset[ModelCapability] = frozenset(
        {ModelCapability.CHAT, ModelCapability.TOOL_CALLING}
    )
    resources: ResourceRequirement = Field(default_factory=ResourceRequirement)
    description: str = ""
    description_ar: str = ""

    @property
    def size_gib(self) -> float:
        return round(self.size_bytes / _GIB, 2)

    @property
    def min_ram_gib(self) -> float:
        return round(self.resources.min_ram_bytes / _GIB, 2)


#: The catalogue. Kept small and honest; every entry is a real, named artifact.
#: Each entry pins the exact artifact: a fixed source URL, the exact byte size
#: and the real SHA-256 of the file at that URL. The installer verifies both, so
#: a truncated, substituted or tampered download is refused rather than loaded.
#: Sizes and hashes were taken from the publishers' LFS metadata; changing a URL
#: without updating the hash is a bug the integrity check will catch.
DEFAULT_CATALOGUE: tuple[CatalogueModel, ...] = (
    CatalogueModel(
        id="qwen2.5-0.5b-instruct-q4_k_m",
        display_name="Qwen2.5 0.5B Instruct (Q4_K_M)",
        display_name_ar="Qwen2.5 ‏0.5B‏ Instruct (Q4_K_M)",
        profile=ModelProfile.LIGHTWEIGHT,
        parameters="0.5B",
        quantization="Q4_K_M",
        url=(
            "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/"
            "qwen2.5-0.5b-instruct-q4_k_m.gguf"
        ),
        filename="qwen2.5-0.5b-instruct-q4_k_m.gguf",
        sha256="74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db",
        size_bytes=491400032,
        context_window=4096,
        capabilities=frozenset(
            {ModelCapability.CHAT, ModelCapability.TOOL_CALLING, ModelCapability.CODE}
        ),
        resources=ResourceRequirement(
            min_ram_bytes=2 * _GIB, disk_bytes=512 * 1024**2, prefers_gpu=False
        ),
        description="Smallest tier. Fast on modest hardware; limited reasoning.",
        description_ar="أصغر فئة. سريع على الأجهزة المتواضعة؛ قدرات استدلال محدودة.",
    ),
    CatalogueModel(
        id="qwen2.5-3b-instruct-q4_k_m",
        display_name="Qwen2.5 3B Instruct (Q4_K_M)",
        display_name_ar="Qwen2.5 ‏3B‏ Instruct (Q4_K_M)",
        profile=ModelProfile.BALANCED,
        parameters="3B",
        quantization="Q4_K_M",
        url=(
            "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/"
            "qwen2.5-3b-instruct-q4_k_m.gguf"
        ),
        filename="qwen2.5-3b-instruct-q4_k_m.gguf",
        sha256="626b4a6678b86442240e33df819e00132d3ba7dddfe1cdc4fbb18e0a9615c62d",
        size_bytes=2104932768,
        context_window=8192,
        capabilities=frozenset(
            {ModelCapability.CHAT, ModelCapability.TOOL_CALLING, ModelCapability.CODE}
        ),
        resources=ResourceRequirement(
            min_ram_bytes=6 * _GIB, disk_bytes=int(2.1 * _GIB), prefers_gpu=True
        ),
        description="Balanced default. Good tool-calling on an 8 GB+ machine.",
        description_ar="الخيار المتوازن الافتراضي. استدعاء أدوات جيد على جهاز ‏8GB+.",
    ),
    CatalogueModel(
        id="qwen2.5-7b-instruct-q4_k_m",
        display_name="Qwen2.5 7B Instruct (Q4_K_M)",
        display_name_ar="Qwen2.5 ‏7B‏ Instruct (Q4_K_M)",
        profile=ModelProfile.HIGH_QUALITY,
        parameters="7B",
        quantization="Q4_K_M",
        url=(
            "https://huggingface.co/lmstudio-community/Qwen2.5-7B-Instruct-GGUF/"
            "resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf"
        ),
        filename="Qwen2.5-7B-Instruct-Q4_K_M.gguf",
        sha256="3e357ab3eda2c442f25c0080bb8998eedc05fd16ce1728eacff9ccc5c3d240c6",
        size_bytes=4683073952,
        context_window=8192,
        capabilities=frozenset(
            {ModelCapability.CHAT, ModelCapability.TOOL_CALLING, ModelCapability.CODE}
        ),
        resources=ResourceRequirement(
            min_ram_bytes=12 * _GIB, disk_bytes=int(4.5 * _GIB), prefers_gpu=True
        ),
        description="High quality. Needs a 16 GB+ machine; a GPU helps.",
        description_ar="جودة عالية. يحتاج جهازًا ‏16GB+‏؛ ويستفيد من كرت رسوميات.",
    ),
)


class Catalogue:
    """An indexed, read-only view over the supported models."""

    def __init__(self, models: tuple[CatalogueModel, ...] = DEFAULT_CATALOGUE) -> None:
        self._models = {m.id: m for m in models}

    def all(self) -> tuple[CatalogueModel, ...]:
        return tuple(self._models.values())

    def get(self, model_id: str) -> CatalogueModel | None:
        return self._models.get(model_id)

    def ids(self) -> list[str]:
        return list(self._models)

    def by_profile(self, profile: ModelProfile) -> tuple[CatalogueModel, ...]:
        return tuple(m for m in self._models.values() if m.profile is profile)


__all__ = ["DEFAULT_CATALOGUE", "Catalogue", "CatalogueModel"]
