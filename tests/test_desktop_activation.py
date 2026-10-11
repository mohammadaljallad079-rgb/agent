"""Model activation and the extended desktop product surface.

These prove the two things the product could not do before:

* a model that is *installed* can be *activated* — health-checked for real and
  made the model the running agent actually uses — and that selection survives a
  restart;
* the desktop UI is backed by real runtime state: the live tool registry, the
  workflow store, the connector catalogue, computer-control settings, the tenant
  memory store, and the model readiness report.

Only the inference engine is replaced (a fake backend bound to a real
descriptor); the platform, policy engine, stores, gateway and HTTP surface are
the real ones.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import SecretStr

from arabic_agent.core.config import Settings
from arabic_agent.core.enums import OperatingMode, RuntimeKind
from arabic_agent.local.main import build_local_control_app
from arabic_agent.models.base import ChatMessage, ModelRequest, ModelResponse, Usage
from arabic_agent.models.registry import ProviderRegistry

from .helpers import FakeProvider, text_response

TOKEN = "control-secret"
HEADERS = {"X-Local-Token": TOKEN}
#: A real catalogue entry, so the manager's own catalogue recognises it.
MODEL_ID = "qwen2.5-0.5b-instruct-q4_k_m"
MODEL_FILENAME = f"{MODEL_ID}.gguf"


class FakeBackend:
    """Deterministic in-process backend; never loads real weights."""

    name = "fake"

    def __init__(self, *, healthy: bool = True) -> None:
        self.loaded = False
        self.closed = False
        self._healthy = healthy

    async def load(self) -> None:
        self.loaded = True

    async def generate(self, request: ModelRequest, *, timeout_seconds: float) -> ModelResponse:
        return ModelResponse(
            message=ChatMessage.assistant("pong"),
            model="fake",
            usage=Usage(prompt_tokens=1, completion_tokens=1),
        )

    async def stream(self, request: ModelRequest, *, timeout_seconds: float):
        yield await self.generate(request, timeout_seconds=timeout_seconds)  # type: ignore[misc]

    async def healthy(self) -> bool:
        return self.loaded and self._healthy and not self.closed

    async def close(self) -> None:
        self.closed = True

    def model_info(self) -> dict[str, object]:
        return {"backend": self.name}


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        require_auth=False,
        runtime_kind=RuntimeKind.LOCAL,
        local_store_dir=str(tmp_path / "data"),
        local_workspace_roots=(str(tmp_path / "approved"),),
        local_mode=OperatingMode.OFFLINE,
        local_control_token=SecretStr(TOKEN),
        max_steps_per_run=20,
        database_url=SecretStr(""),
        redis_url=SecretStr(""),
        execution_mode="inline",
    )


def _install_fake_model(tmp_path: Path) -> str:
    """Write a real catalogue model's file + manifest entry so it is installed.

    The weights are a stub (the backend is faked), but the id, filename and
    manifest are exactly what the manager looks for, so installation is proven
    against the real catalogue rather than a test-only one.
    """
    from arabic_agent.local.model_install import _write_manifest

    storage = tmp_path / "data" / "models"
    storage.mkdir(parents=True, exist_ok=True)
    (storage / MODEL_FILENAME).write_bytes(b"GGUF" + b"\x00" * 1024)
    _write_manifest(
        storage,
        {MODEL_ID: {"path": MODEL_FILENAME, "size_bytes": 1028, "sha256": None}},
    )
    return str(storage)


def _app(tmp_path: Path, *, backend_healthy: bool = True):  # type: ignore[no-untyped-def]
    _install_fake_model(tmp_path)
    registry = ProviderRegistry()
    registry.register(FakeProvider(lambda req, n: text_response("تم")))
    app = build_local_control_app(
        _settings(tmp_path), provider_registry=registry, default_model="fake/model"
    )
    service = app.state.desktop_service

    # Each factory call builds a fresh provider over a fresh backend, exactly as
    # the real desktop does (one engine handle per provider).
    def _factory(model_id: str):  # type: ignore[no-untyped-def]
        import arabic_agent.local.model as local_model

        def _build():  # type: ignore[no-untyped-def]
            backend = FakeBackend(healthy=backend_healthy)
            with patch.object(local_model, "build_backend", lambda descriptor, **kw: backend):
                return local_model.LocalBackendProvider(
                    descriptor=service.model_manager.descriptor_for(model_id), backend=backend
                )

        return _build

    service._provider_factory = _factory  # type: ignore[assignment]
    return app


# --------------------------------------------------------------------------
# Model activation end to end
# --------------------------------------------------------------------------
def test_models_endpoint_reports_installed_and_no_active(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    body = client.get("/desktop/models", headers=HEADERS).json()
    model = next(m for m in body["models"] if m["id"] == MODEL_ID)
    assert model["installed"] is True
    assert model["active"] is False
    assert body["active"]["selected"] is False


def test_activate_installed_model_switches_the_gateway_default(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    response = client.post(f"/desktop/models/{MODEL_ID}/activate", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["active"] is True
    assert body["health"]["health"] == "healthy"
    # The gateway now runs the activated local model, not the injected default.
    gateway = app.state.desktop_service._platform.gateway
    assert gateway.default_model.startswith("local/")
    assert gateway.default_model.endswith(".gguf")


def test_activate_persists_across_restart(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    client.post(f"/desktop/models/{MODEL_ID}/activate", headers=HEADERS)

    # A fresh app over the same store must report the model as already active.
    registry = ProviderRegistry()
    registry.register(FakeProvider(lambda req, n: text_response("تم")))
    app2 = build_local_control_app(
        _settings(tmp_path), provider_registry=registry, default_model="fake/model"
    )
    body = TestClient(app2).get("/desktop/models", headers=HEADERS).json()
    assert body["active"]["selected"] is True
    assert body["active"]["model_id"] == MODEL_ID


def test_activation_refused_when_health_check_fails(tmp_path):
    app = _app(tmp_path, backend_healthy=False)
    client = TestClient(app)
    body = client.post(f"/desktop/models/{MODEL_ID}/activate", headers=HEADERS).json()
    assert body["active"] is False
    assert "health" in body["reason"] or body["health"]["health"] != "healthy"


def test_activate_uninstalled_model_is_not_found(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    response = client.post("/desktop/models/does-not-exist/activate", headers=HEADERS)
    assert response.status_code == 404


def test_deactivate_clears_selection(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    client.post(f"/desktop/models/{MODEL_ID}/activate", headers=HEADERS)
    body = client.post("/desktop/models/active/deactivate", headers=HEADERS).json()
    assert body["previous_model_id"] == MODEL_ID
    after = client.get("/desktop/models", headers=HEADERS).json()
    assert after["active"]["selected"] is False


def test_activation_is_audited(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    client.post(f"/desktop/models/{MODEL_ID}/activate", headers=HEADERS)
    client.post("/desktop/models/active/deactivate", headers=HEADERS)
    # The broker is a real audit sink; it mirrors what was recorded.
    broker = app.state.desktop_events
    types = {event.type for event in broker.recent(limit=100)}
    assert "model.activated" in types
    assert "model.deactivated" in types


def test_readiness_reports_not_ready_without_active_model(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    body = client.get("/desktop/models/readiness", headers=HEADERS).json()
    assert body["ready"] is False
    assert body["next_step"] == "install_and_activate"


def test_readiness_reports_ready_after_activation(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    client.post(f"/desktop/models/{MODEL_ID}/activate", headers=HEADERS)
    body = client.get("/desktop/models/readiness", headers=HEADERS).json()
    assert body["ready"] is True, body
    assert body["model_id"] == MODEL_ID


# --------------------------------------------------------------------------
# Extended desktop surface: real runtime state
# --------------------------------------------------------------------------
def test_tools_endpoint_lists_the_live_registry(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    body = client.get("/desktop/tools", headers=HEADERS).json()
    names = {tool["name"] for tool in body["tools"]}
    assert body["count"] == len(body["tools"])
    assert names, "the real tool registry must not be empty"
    # Permissions are reported per tool, from the tool itself.
    assert all(isinstance(tool["permissions"], list) for tool in body["tools"])


def test_workflows_endpoint_is_empty_but_real(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    body = client.get("/desktop/workflows", headers=HEADERS).json()
    assert body["workflows"] == []


def test_connectors_and_computer_and_memory_endpoints(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    connectors = client.get("/desktop/connectors", headers=HEADERS).json()
    assert "enabled" in connectors and "connectors" in connectors
    computer = client.get("/desktop/computer", headers=HEADERS).json()
    assert computer["enabled"] is False  # default setting, honestly reported
    memory = client.get("/desktop/memory", headers=HEADERS).json()
    assert memory["entries"] == []


def test_bootstrap_includes_the_extended_surface(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    body = client.get("/desktop/bootstrap", headers=HEADERS).json()
    expected = (
        "readiness",
        "tools",
        "workflows",
        "connectors",
        "computer",
        "memory",
        "active_model",
    )
    for key in expected:
        assert key in body, f"bootstrap must expose {key}"


def test_providers_never_leak_secrets(tmp_path):
    app = _app(tmp_path)
    client = TestClient(app)
    body = client.get("/desktop/providers", headers=HEADERS).json()
    assert "needs_setup" in body
    for provider in body["providers"]:
        assert "api_key" not in provider
        assert "configured" in provider
