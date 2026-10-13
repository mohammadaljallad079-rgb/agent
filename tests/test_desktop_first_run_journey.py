"""Phase 10D: the complete desktop first-run journey on a real model.

The other desktop tests prove individual pieces against a faked inference
backend and a tiny stub artifact. This module proves the *whole* first-run
journey a user actually walks, with nothing faked except the model's source URL:

    fresh app data
      -> hardware detection
      -> catalogue listing + recommendation
      -> real streamed download of a genuine GGUF (the operator-approved URL is
         pointed at the real file by a local HTTP server, so the installer's
         streaming, size check and SHA-256 check all run for real)
      -> install into the model store + manifest
      -> activation (real load + real health check)
      -> routed inference acceptance (real output through the live gateway)
      -> persistence of the activation across a restart

The model is the real ``AAP_TEST_MODEL_PATH`` GGUF, and the backend is the real
``llama-cpp-python`` engine — no ``FakeBackend``. It is opt-in
(``@pytest.mark.local_model``) and skipped when no model file is present.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from arabic_agent.core.config import Settings
from arabic_agent.core.enums import ModelProfile, OperatingMode, RuntimeKind
from arabic_agent.desktop.model_catalogue import (
    Catalogue,
    CatalogueModel,
    ResourceRequirement,
)
from arabic_agent.desktop.model_manager import ModelManager, read_active_model
from arabic_agent.local.hardware import detect_hardware
from arabic_agent.local.main import build_local_control_app
from arabic_agent.models.registry import ProviderRegistry

from .fake_http import http_server
from .helpers import FakeProvider, text_response

MODEL_PATH = os.environ.get("AAP_TEST_MODEL_PATH", "")
requires_model = pytest.mark.skipif(
    not MODEL_PATH or not Path(MODEL_PATH).is_file(),
    reason="AAP_TEST_MODEL_PATH not set to a readable GGUF file",
)
pytestmark = pytest.mark.local_model

#: The real catalogue id whose filename the model file carries.
MODEL_ID = "qwen2.5-3b-instruct-q4_k_m"
MODEL_FILENAME = f"{MODEL_ID}.gguf"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        require_auth=False,
        runtime_kind=RuntimeKind.LOCAL,
        local_store_dir=str(tmp_path / "data"),
        local_workspace_roots=(str(tmp_path / "approved"),),
        local_mode=OperatingMode.OFFLINE,
        local_control_token=SecretStr("control-secret"),
        max_steps_per_run=20,
        database_url=SecretStr(""),
        redis_url=SecretStr(""),
        execution_mode="inline",
        # The model store lives in the fresh app data dir, exactly like a real
        # first run on a machine that has never installed a model.
        local_model_storage_dir=str(tmp_path / "data" / "models"),
    )


def _app(tmp_path: Path, server_base_url: str):
    """Build the real desktop app whose catalogue serves the real GGUF locally.

    Only the *source URL* is redirected to the local server; the size and the
    SHA-256 are the genuine values of the real file, so the installer verifies
    the real bytes rather than a stub.
    """
    real_size = Path(MODEL_PATH).stat().st_size
    import hashlib

    digest = hashlib.sha256()
    with open(MODEL_PATH, "rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    real_sha = digest.hexdigest()

    model = CatalogueModel(
        id=MODEL_ID,
        display_name="Qwen2.5 3B Instruct (Q4_K_M)",
        display_name_ar="Qwen2.5 3B",
        profile=ModelProfile.BALANCED,
        parameters="3B",
        quantization="Q4_K_M",
        url=f"{server_base_url}/{MODEL_FILENAME}",
        filename=MODEL_FILENAME,
        sha256=real_sha,
        size_bytes=real_size,
        context_window=2048,
        resources=ResourceRequirement(min_ram_bytes=1 * 1024**3, disk_bytes=real_size),
    )
    registry = ProviderRegistry()
    # A remote provider is registered as a fallback, but the run is offline and
    # local-only: it must never be reached.
    registry.register(FakeProvider(lambda req, n: text_response("REMOTE")))
    app = build_local_control_app(
        _settings(tmp_path), provider_registry=registry, default_model="fake/model"
    )
    service = app.state.desktop_service
    storage = str(tmp_path / "data" / "models")
    service._models = ModelManager(  # type: ignore[assignment]
        storage_dir=storage,
        catalogue=Catalogue((model,)),
        hardware=detect_hardware(disk_path=storage),
    )
    return app, service


@pytest.mark.local_model
@requires_model
async def test_first_run_journey_installs_activates_and_runs_real_model(tmp_path: Path):
    """The whole first-run path, from empty app data to a verified real answer."""
    with http_server() as server:
        server.route_file(f"/{MODEL_FILENAME}", MODEL_PATH)
        app, service = _app(tmp_path, server.base_url)

        storage = tmp_path / "data" / "models"
        # --- 1. A fresh install has nothing installed and nothing active. -----
        assert read_active_model(storage) is None
        listed = service.list_models()
        assert len(listed) == 1
        assert listed[0]["installed"] is False
        readiness = await service.model_readiness()
        assert readiness["ready"] is False
        assert readiness["next_step"] in {"install_and_activate", "install"}

        # --- 2. Hardware detection produced a real, usable profile. -----------
        assert service.model_manager._hardware is not None
        assert service.model_manager._hardware.total_ram_bytes > 0

        # --- 3. Recommendation picks the only runnable model. -----------------
        recommendation = service.recommend_model()
        assert recommendation["model_id"] == MODEL_ID

        # --- 4. The real installer downloads the genuine 2.1 GB GGUF. ---------
        install = await service.install_model(MODEL_ID)
        result = install["install"]
        assert result["success"] is True, result
        installed_file = storage / MODEL_FILENAME
        assert installed_file.is_file()
        assert result["size_bytes"] == install["model"]["size_bytes"]
        assert install["progress"]["state"] == "installed"

        # The manifest records the real artifact, so a later verify can use it.
        listed = service.list_models()
        assert listed[0]["installed"] is True

        # --- 5. Activation loads the real weights and health-checks them. -----
        activation = await service.activate_model(MODEL_ID)
        assert activation["active"] is True, activation
        assert activation["health"]["health"] == "healthy", activation["health"]
        assert read_active_model(storage) == MODEL_ID

        # --- 6. Routed inference acceptance: real output via the live gateway. -
        acceptance = await service.test_model_inference(MODEL_ID)
        assert acceptance["passed"] is True, acceptance
        assert acceptance["output_chars"] > 0
        assert acceptance["usage"]["completion_tokens"] >= 1

        # --- 7. Readiness is now genuinely ready. -----------------------------
        readiness = await service.model_readiness()
        assert readiness["ready"] is True, readiness
        assert readiness["next_step"] == ""

        # --- 8. The activation persists across a restart. ---------------------
        # A restart rebuilds the manager from settings through the real
        # ``build_model_manager`` path, which reads the persisted activation.
        from arabic_agent.desktop.service import build_model_manager

        restarted = build_model_manager(service._platform)
        assert restarted.active_model_id() == MODEL_ID
        assert restarted.can_activate(MODEL_ID)["ok"] is True

        await app.state.desktop_service._platform.aclose()


@pytest.mark.local_model
@requires_model
async def test_first_run_journey_is_offline_and_local_only(tmp_path: Path):
    """No step of the first-run journey may reach a remote model."""
    with http_server() as server:
        server.route_file(f"/{MODEL_FILENAME}", MODEL_PATH)
        _app_obj, service = _app(tmp_path, server.base_url)
        platform = service._platform
        try:
            await service.install_model(MODEL_ID)
            await service.activate_model(MODEL_ID)
            await service.test_model_inference(MODEL_ID)
            assert platform.gateway.stats.remote_requests == 0
            assert platform.gateway.stats.local_requests >= 1
        finally:
            await platform.aclose()


@pytest.mark.local_model
@requires_model
async def test_installed_model_is_rejected_when_bytes_are_tampered(tmp_path: Path):
    """The real SHA-256 check refuses a corrupted artifact, not just a stub one."""
    with http_server() as server:
        # Serve a *different* (small) file at the real model's URL: the real
        # SHA-256/size of the 3B model will not match, so the install must fail
        # and leave no promoted file.
        bad = tmp_path / "bad.gguf"
        bad.write_bytes(b"GGUF" + b"\x00" * 4096)
        server.route_file(f"/{MODEL_FILENAME}", str(bad))
        _app_obj, service = _app(tmp_path, server.base_url)
        try:
            install = await service.install_model(MODEL_ID)
            assert install["install"]["success"] is False
            assert install["progress"]["state"] == "failed"
            assert not (tmp_path / "data" / "models" / MODEL_FILENAME).exists()
        finally:
            await service._platform.aclose()
