"""Local AI / Model Manager tests.

These prove the manager's honesty: installed is not healthy, a recommendation
never proposes a tier the hardware gate rejects, installation verifies integrity,
removal refuses to touch the active model, and every operation stays inside the
model store. Installation is exercised against a real local HTTP server.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arabic_agent.core.enums import ModelHealth, ModelProfile, PlatformKind
from arabic_agent.core.errors import ConflictError, NotFoundError, ValidationError
from arabic_agent.desktop.model_catalogue import Catalogue, CatalogueModel
from arabic_agent.desktop.model_manager import ModelManager
from arabic_agent.local.hardware import HardwareProfile, ResourceRequirement
from arabic_agent.local.model_install import _write_manifest, read_manifest

from .fake_http import http_server

_GIB = 1024**3


def _model(
    model_id: str, profile: ModelProfile, *, size: int = 100, min_ram: int = _GIB
) -> CatalogueModel:
    return CatalogueModel(
        id=model_id,
        display_name=model_id,
        profile=profile,
        url=f"https://example.invalid/{model_id}.gguf",
        filename=f"{model_id}.gguf",
        size_bytes=size,
        resources=ResourceRequirement(min_ram_bytes=min_ram, disk_bytes=size),
    )


def _hardware(*, ram_gib: float, disk_gib: float = 100.0, gpu: bool = False) -> HardwareProfile:
    return HardwareProfile(
        platform=PlatformKind.LINUX,
        architecture="x86_64",
        cpu_count=8,
        total_ram_bytes=int(ram_gib * _GIB),
        disk_free_bytes=int(disk_gib * _GIB),
        gpu_available=gpu,
    )


def _manager(tmp_path: Path, **kwargs: object) -> ModelManager:
    return ModelManager(storage_dir=str(tmp_path / "models"), **kwargs)  # type: ignore[arg-type]


def test_list_models_reports_catalogue_without_claiming_installed(tmp_path):
    manager = _manager(tmp_path)
    models = manager.list_models()
    assert len(models) == len(Catalogue().all())
    assert all(m["installed"] is False for m in models)
    assert all(m["path"] is None for m in models)
    # Every entry has an Arabic display name (Arabic-first, not a later translation).
    assert all(m["display_name_ar"] for m in models)


def test_recommend_without_hardware_is_a_default_not_a_measurement(tmp_path):
    manager = _manager(tmp_path)
    rec = manager.recommend()
    assert rec["profile"] == ModelProfile.BALANCED.value
    assert "default" in str(rec["reason"])


def test_recommend_on_small_machine_chooses_lightweight(tmp_path):
    manager = _manager(tmp_path, hardware=_hardware(ram_gib=2.5))
    rec = manager.recommend()
    assert rec["profile"] == ModelProfile.LIGHTWEIGHT.value
    # The high-quality tier must be reported unrunnable, with a reason.
    assert rec["verdicts"][ModelProfile.HIGH_QUALITY.value]["runnable"] is False
    assert rec["verdicts"][ModelProfile.HIGH_QUALITY.value]["reason"]


def test_recommend_on_large_machine_chooses_high_quality(tmp_path):
    manager = _manager(tmp_path, hardware=_hardware(ram_gib=32))
    rec = manager.recommend()
    assert rec["profile"] == ModelProfile.HIGH_QUALITY.value
    assert rec["verdicts"][ModelProfile.HIGH_QUALITY.value]["runnable"] is True


async def test_install_downloads_verifies_and_manifests(tmp_path):
    payload = b"GGUF" + b"\x00" * 4096
    with http_server() as server:
        server.route("/m.gguf", body=payload)
        model = CatalogueModel(
            id="test-model",
            display_name="Test",
            profile=ModelProfile.LIGHTWEIGHT,
            url=f"{server.base_url}/m.gguf",
            filename="test-model.gguf",
            size_bytes=len(payload),
            resources=ResourceRequirement(min_ram_bytes=_GIB, disk_bytes=len(payload)),
        )
        manager = ModelManager(
            storage_dir=str(tmp_path / "models"),
            catalogue=Catalogue((model,)),
            hardware=_hardware(ram_gib=16),
        )
        result = await manager.install("test-model")

    assert result["install"]["success"] is True
    assert result["progress"]["state"] == "installed"
    manifest = read_manifest(Path(tmp_path / "models"))
    assert "test-model" in manifest
    assert manifest["test-model"]["size_bytes"] == len(payload)


async def test_install_rejects_when_hardware_cannot_run(tmp_path):
    with http_server() as server:
        server.route("/m.gguf", body=b"x")
        model = CatalogueModel(
            id="big",
            display_name="Big",
            profile=ModelProfile.HIGH_QUALITY,
            url=f"{server.base_url}/m.gguf",
            filename="big.gguf",
            size_bytes=1,
            resources=ResourceRequirement(min_ram_bytes=64 * _GIB, disk_bytes=1),
        )
        manager = ModelManager(
            storage_dir=str(tmp_path / "models"),
            catalogue=Catalogue((model,)),
            hardware=_hardware(ram_gib=4),
        )
        with pytest.raises(ValidationError):
            await manager.install("big")


async def test_install_unknown_model_raises_not_found(tmp_path):
    manager = _manager(tmp_path)
    with pytest.raises(NotFoundError):
        await manager.install("does-not-exist")


async def test_install_integrity_failure_leaves_no_model(tmp_path):
    payload = b"wrong-content"
    with http_server() as server:
        server.route("/m.gguf", body=payload)
        model = CatalogueModel(
            id="hashed",
            display_name="Hashed",
            profile=ModelProfile.LIGHTWEIGHT,
            url=f"{server.base_url}/m.gguf",
            filename="hashed.gguf",
            size_bytes=len(payload),
            sha256="0" * 64,
            resources=ResourceRequirement(min_ram_bytes=_GIB, disk_bytes=len(payload)),
        )
        manager = ModelManager(
            storage_dir=str(tmp_path / "models"),
            catalogue=Catalogue((model,)),
            hardware=_hardware(ram_gib=16),
        )
        result = await manager.install("hashed")
    assert result["install"]["success"] is False
    assert result["progress"]["state"] == "failed"
    assert "hashed" not in read_manifest(Path(tmp_path / "models"))


async def test_health_check_without_factory_is_unknown(tmp_path):
    """A file existing is not a health check: without a provider it is unknown."""
    storage = tmp_path / "models"
    storage.mkdir(parents=True)
    (storage / "installed.gguf").write_bytes(b"x")
    _write_manifest(
        storage,
        {"installed": {"path": "installed.gguf", "size_bytes": 1, "sha256": None}},
    )
    model = CatalogueModel(
        id="installed",
        display_name="Installed",
        profile=ModelProfile.LIGHTWEIGHT,
        url="https://example.invalid/i.gguf",
        filename="installed.gguf",
    )
    manager = ModelManager(storage_dir=str(storage), catalogue=Catalogue((model,)))
    result = await manager.health_check("installed")
    assert result["health"] == ModelHealth.UNKNOWN.value


async def test_health_check_with_fake_provider_reports_healthy(tmp_path):
    storage = tmp_path / "models"
    storage.mkdir(parents=True)
    (storage / "m.gguf").write_bytes(b"x")
    _write_manifest(storage, {"m": {"path": "m.gguf", "size_bytes": 1, "sha256": None}})
    model = CatalogueModel(
        id="m",
        display_name="M",
        profile=ModelProfile.LIGHTWEIGHT,
        url="https://example.invalid/m.gguf",
        filename="m.gguf",
    )
    manager = ModelManager(storage_dir=str(storage), catalogue=Catalogue((model,)))

    class _Provider:
        async def load(self) -> None:
            return None

        async def probe(self) -> bool:
            return True

        async def complete(self, request: object) -> object:
            from arabic_agent.models.base import ChatMessage, ModelResponse

            return ModelResponse(message=ChatMessage.assistant("جاهز"), model="fake")

        async def aclose(self) -> None:
            return None

    result = await manager.health_check("m", provider_factory=_Provider)
    assert result["health"] == ModelHealth.HEALTHY.value


async def test_health_check_failure_is_unhealthy_not_raised(tmp_path):
    storage = tmp_path / "models"
    storage.mkdir(parents=True)
    (storage / "m.gguf").write_bytes(b"x")
    _write_manifest(storage, {"m": {"path": "m.gguf", "size_bytes": 1, "sha256": None}})
    model = CatalogueModel(
        id="m",
        display_name="M",
        profile=ModelProfile.LIGHTWEIGHT,
        url="https://example.invalid/m.gguf",
        filename="m.gguf",
    )
    manager = ModelManager(storage_dir=str(storage), catalogue=Catalogue((model,)))

    class _Broken:
        async def load(self) -> None:
            raise RuntimeError("boom")

    result = await manager.health_check("m", provider_factory=_Broken)
    assert result["health"] == ModelHealth.UNHEALTHY.value


def test_descriptor_for_installed_model(tmp_path):
    storage = tmp_path / "models"
    storage.mkdir(parents=True)
    (storage / "m.gguf").write_bytes(b"x")
    _write_manifest(storage, {"m": {"path": "m.gguf", "size_bytes": 1, "sha256": None}})
    model = CatalogueModel(
        id="m",
        display_name="M",
        profile=ModelProfile.LIGHTWEIGHT,
        url="https://example.invalid/m.gguf",
        filename="m.gguf",
    )
    manager = ModelManager(storage_dir=str(storage), catalogue=Catalogue((model,)))
    descriptor = manager.descriptor_for("m")
    assert descriptor is not None
    assert descriptor.model_path.endswith("m.gguf")
    assert manager.descriptor_for("missing") is None


def test_remove_active_model_is_refused(tmp_path):
    storage = tmp_path / "models"
    storage.mkdir(parents=True)
    (storage / "m.gguf").write_bytes(b"x")
    _write_manifest(storage, {"m": {"path": "m.gguf", "size_bytes": 1, "sha256": None}})
    model = CatalogueModel(
        id="m",
        display_name="M",
        profile=ModelProfile.LIGHTWEIGHT,
        url="https://example.invalid/m.gguf",
        filename="m.gguf",
    )
    manager = ModelManager(
        storage_dir=str(storage), catalogue=Catalogue((model,)), active_model_id="m"
    )
    with pytest.raises(ConflictError):
        manager.remove("m")


def test_remove_installed_model_deletes_file_and_manifest(tmp_path):
    storage = tmp_path / "models"
    storage.mkdir(parents=True)
    (storage / "m.gguf").write_bytes(b"x" * 10)
    _write_manifest(storage, {"m": {"path": "m.gguf", "size_bytes": 10, "sha256": None}})
    model = CatalogueModel(
        id="m",
        display_name="M",
        profile=ModelProfile.LIGHTWEIGHT,
        url="https://example.invalid/m.gguf",
        filename="m.gguf",
    )
    manager = ModelManager(storage_dir=str(storage), catalogue=Catalogue((model,)))
    result = manager.remove("m")
    assert result["removed"] is True
    assert result["freed_bytes"] == 10
    assert not (storage / "m.gguf").exists()
    assert "m" not in read_manifest(storage)


def test_remove_not_installed_raises(tmp_path):
    manager = _manager(tmp_path)
    with pytest.raises(NotFoundError):
        manager.remove("nope")


def test_verify_installed_model(tmp_path):
    storage = tmp_path / "models"
    storage.mkdir(parents=True)
    (storage / "m.gguf").write_bytes(b"x")
    _write_manifest(storage, {"m": {"path": "m.gguf", "size_bytes": 1, "sha256": None}})
    model = CatalogueModel(
        id="m",
        display_name="M",
        profile=ModelProfile.LIGHTWEIGHT,
        url="https://example.invalid/m.gguf",
        filename="m.gguf",
    )
    manager = ModelManager(storage_dir=str(storage), catalogue=Catalogue((model,)))
    result = manager.verify("m")
    assert result["success"] is True


# --------------------------------------------------------------------------
# Activation: install -> select -> run
# --------------------------------------------------------------------------
def test_activate_requires_installed_model(tmp_path):
    """A model that is not installed cannot be activated."""
    manager = _manager(tmp_path)
    verdict = manager.can_activate("qwen2.5-0.5b-instruct-q4_k_m")
    assert verdict["ok"] is False
    assert "not installed" in str(verdict["reason"])
    with pytest.raises(NotFoundError):
        manager.activate("qwen2.5-0.5b-instruct-q4_k_m")


def test_activate_unknown_model_is_refused(tmp_path):
    manager = _manager(tmp_path)
    assert manager.can_activate("no-such-model")["ok"] is False


def test_activate_installed_model_sets_and_persists(tmp_path):
    storage = tmp_path / "models"
    storage.mkdir(parents=True)
    (storage / "m.gguf").write_bytes(b"x")
    _write_manifest(storage, {"m": {"path": "m.gguf", "size_bytes": 1, "sha256": None}})
    model = CatalogueModel(
        id="m",
        display_name="M",
        profile=ModelProfile.LIGHTWEIGHT,
        url="https://example.invalid/m.gguf",
        filename="m.gguf",
    )
    persisted: list[str] = []
    manager = ModelManager(storage_dir=str(storage), catalogue=Catalogue((model,)))
    result = manager.activate("m", persist=persisted.append)
    assert result["active"] is True
    assert manager.active_model_id() == "m"
    assert persisted == ["m"]
    assert next(m for m in manager.list_models() if m["active"])["id"] == "m"


def test_active_model_persists_across_rebuild(tmp_path):
    """read/write_active_model round-trips so a restart keeps the selection."""
    from arabic_agent.desktop.model_manager import read_active_model, write_active_model

    storage = tmp_path / "models"
    assert read_active_model(storage) is None
    write_active_model(storage, "m")
    assert read_active_model(storage) == "m"
    write_active_model(storage, None)
    assert read_active_model(storage) is None


def test_deactivate_clears_active(tmp_path):
    storage = tmp_path / "models"
    storage.mkdir(parents=True)
    (storage / "m.gguf").write_bytes(b"x")
    _write_manifest(storage, {"m": {"path": "m.gguf", "size_bytes": 1, "sha256": None}})
    model = CatalogueModel(
        id="m",
        display_name="M",
        profile=ModelProfile.LIGHTWEIGHT,
        url="https://example.invalid/m.gguf",
        filename="m.gguf",
    )
    manager = ModelManager(
        storage_dir=str(storage), catalogue=Catalogue((model,)), active_model_id="m"
    )
    assert manager.deactivate()["previous_model_id"] == "m"
    assert manager.active_model_id() is None


def test_active_descriptor_is_none_without_selection(tmp_path):
    manager = _manager(tmp_path)
    assert manager.active_model_id() is None
    assert manager.active_descriptor() is None
