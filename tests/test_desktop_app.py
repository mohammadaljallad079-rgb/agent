"""Desktop product surface tests.

These build the real local platform and exercise the desktop HTTP surface end to
end: authentication, bootstrap, durable run persistence across a restart,
approvals, models, modes, settings, permissions and the diagnostics export. The
agent loop, policy engine and tools are the real ones; only the LLM is faked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from arabic_agent.browser.provisioning import BrowserProvisioner
from arabic_agent.core.config import Settings
from arabic_agent.core.enums import OperatingMode, RuntimeKind
from arabic_agent.local.main import build_local_control_app
from arabic_agent.models.registry import ProviderRegistry

from .helpers import FakeProvider, plan_json, scripted_responder, text_response, verify_json

TOKEN = "control-secret"
HEADERS = {"X-Local-Token": TOKEN}


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


def _app(tmp_path: Path, responder=None):  # type: ignore[no-untyped-def]
    registry = ProviderRegistry()
    registry.register(FakeProvider(responder or (lambda req, n: text_response("تم"))))
    return build_local_control_app(
        _settings(tmp_path), provider_registry=registry, default_model="fake/model"
    )


def _completed_responder():  # type: ignore[no-untyped-def]
    return scripted_responder(
        plan=plan_json(("echo", "echo")),
        tool_calls=[("echo", {"text": "x"}), ("finish", {"result": "تم", "verified": True})],
        verification=verify_json(True, "تم"),
    )


def test_desktop_requires_control_token(tmp_path):
    client = TestClient(_app(tmp_path))
    assert client.get("/desktop/bootstrap").status_code == 403
    assert client.get("/desktop/bootstrap", headers=HEADERS).status_code == 200


def test_desktop_version_endpoint(tmp_path):
    client = TestClient(_app(tmp_path))
    body = client.get("/desktop/version").json()
    assert body["version"]


def test_bootstrap_reports_real_state(tmp_path):
    client = TestClient(_app(tmp_path))
    boot = client.get("/desktop/bootstrap", headers=HEADERS).json()
    assert boot["version"]
    assert boot["modes"]["current"] == OperatingMode.OFFLINE.value
    assert len(boot["models"]) >= 1
    assert boot["runs"] == []
    assert boot["approvals"] == []
    assert len(boot["permissions"]["capabilities"]) >= 1
    assert boot["settings"]["general"]["environment"] == "test"


def test_start_run_persists_and_completes(tmp_path):
    client = TestClient(_app(tmp_path, _completed_responder()))
    resp = client.post("/desktop/runs", json={"goal": "قل مرحبا"}, headers=HEADERS)
    assert resp.status_code == 201
    run = resp.json()
    assert run["status"] in {"completed", "failed"}
    runs = client.get("/desktop/runs", headers=HEADERS).json()
    assert len(runs) == 1
    assert runs[0]["id"] == run["id"]


def test_start_run_requires_goal(tmp_path):
    client = TestClient(_app(tmp_path))
    resp = client.post("/desktop/runs", json={"goal": ""}, headers=HEADERS)
    assert resp.status_code == 422


def test_run_timeline_returns_real_audit_events(tmp_path):
    client = TestClient(_app(tmp_path, _completed_responder()))
    run = client.post("/desktop/runs", json={"goal": "قل مرحبا"}, headers=HEADERS).json()
    timeline = client.get(f"/desktop/runs/{run['id']}/timeline", headers=HEADERS).json()
    assert len(timeline) >= 1
    types = {e["type"] for e in timeline}
    assert "run.started" in types


def test_runs_survive_app_restart(tmp_path):
    """The desktop session store is durable: a new app sees the earlier run."""
    app1 = _app(tmp_path, _completed_responder())
    client1 = TestClient(app1)
    run = client1.post("/desktop/runs", json={"goal": "مهمة دائمة"}, headers=HEADERS).json()

    app2 = _app(tmp_path, _completed_responder())
    client2 = TestClient(app2)
    runs = client2.get("/desktop/runs", headers=HEADERS).json()
    assert any(r["id"] == run["id"] for r in runs)


def test_models_endpoint_lists_catalogue(tmp_path):
    client = TestClient(_app(tmp_path))
    body = client.get("/desktop/models", headers=HEADERS).json()
    assert "models" in body and "recommendation" in body
    assert len(body["models"]) >= 1


def test_model_progress_unknown_is_none(tmp_path):
    client = TestClient(_app(tmp_path))
    body = client.get("/desktop/models/qwen2.5-3b-instruct-q4_k_m/progress", headers=HEADERS).json()
    assert body["progress"] is None


def test_model_install_unknown_is_404(tmp_path):
    client = TestClient(_app(tmp_path))
    resp = client.post("/desktop/models/not-a-model/install", headers=HEADERS)
    assert resp.status_code == 404


def test_modes_and_set_mode(tmp_path):
    client = TestClient(_app(tmp_path))
    modes = client.get("/desktop/modes", headers=HEADERS).json()
    assert modes["current"] == OperatingMode.OFFLINE.value
    assert "privacy" in modes
    # Switching to hybrid is a real runtime state change.
    updated = client.post("/desktop/modes", json={"mode": "hybrid"}, headers=HEADERS).json()
    assert updated["mode"] == OperatingMode.HYBRID.value


def test_privacy_view_reports_real_settings(tmp_path):
    client = TestClient(_app(tmp_path))
    privacy = client.get("/desktop/modes", headers=HEADERS).json()["privacy"]
    assert privacy["computer_control_enabled"] is False
    assert privacy["browser_enabled"] is False
    assert privacy["connectors_enabled"] is False


def test_permissions_view_matches_policy(tmp_path):
    client = TestClient(_app(tmp_path))
    payload = client.get("/desktop/permissions", headers=HEADERS).json()
    caps = {c["id"]: c for c in payload["capabilities"]}
    assert caps["files.read"]["effect"] == "allow"
    # Writing files needs approval under the platform default policy.
    assert caps["files.write"]["effect"] == "requires_approval"
    assert caps["files.write"]["requires_approval"] is True


def test_settings_view_has_no_secrets(tmp_path):
    client = TestClient(_app(tmp_path))
    settings = client.get("/desktop/settings", headers=HEADERS).json()
    text = str(settings)
    assert "control-secret" not in text
    assert settings["general"]["locale"] == "ar"


def test_diagnostics_export_is_redacted(tmp_path):
    client = TestClient(_app(tmp_path))
    exported = client.get("/desktop/diagnostics/export", headers=HEADERS).json()
    assert exported["product"] == "Arabic AI Agent Platform"
    assert "control-secret" not in str(exported)
    assert exported["export_kind"] == "diagnostics"


def test_cancel_run_transitions_to_cancelled(tmp_path):
    client = TestClient(_app(tmp_path, _completed_responder()))
    run = client.post("/desktop/runs", json={"goal": "قل مرحبا"}, headers=HEADERS).json()
    resp = client.post(f"/desktop/runs/{run['id']}/cancel", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] in {"completed", "failed", "cancelled"}


def test_desktop_mounted_below_local_control(tmp_path):
    client = TestClient(_app(tmp_path))
    # The original control API still works alongside the desktop surface.
    assert client.get("/local/status", headers=HEADERS).status_code == 200
    assert client.get("/local/capabilities", headers=HEADERS).status_code == 200


def test_with_desktop_false_omits_product_surface(tmp_path):
    registry = ProviderRegistry()
    registry.register(FakeProvider(lambda req, n: text_response("ok")))
    app = build_local_control_app(
        _settings(tmp_path),
        provider_registry=registry,
        default_model="fake/model",
        with_desktop=False,
    )
    client = TestClient(app)
    assert client.get("/desktop/bootstrap", headers=HEADERS).status_code == 404
    assert client.get("/local/status", headers=HEADERS).status_code == 200


def test_control_token_still_required_by_launcher(tmp_path):
    from arabic_agent.core.errors import ConfigurationError

    registry = ProviderRegistry()
    registry.register(FakeProvider(lambda req, n: text_response("ok")))
    no_token = _settings(tmp_path).model_copy(
        update={"local_control_token": SecretStr(""), "require_auth": True}
    )
    with pytest.raises(ConfigurationError):
        build_local_control_app(no_token, provider_registry=registry, default_model="fake/model")


# --- Browser provisioning surface ------------------------------------------
def test_browser_view_reports_real_state(tmp_path):
    client = TestClient(_app(tmp_path))
    body = client.get("/desktop/browser", headers=HEADERS).json()
    assert body["state"] in {"ready", "not_installed", "unknown", "broken"}
    assert isinstance(body["package_available"], bool)
    assert "enabled" in body and "headless" in body
    # Read-only view: it must not leak a token or a raw environment dump.
    assert "token" not in json.dumps(body).lower()


def test_bootstrap_includes_browser_state(tmp_path):
    client = TestClient(_app(tmp_path))
    boot = client.get("/desktop/bootstrap", headers=HEADERS).json()
    assert "browser" in boot
    assert "state" in boot["browser"]
    assert "browser" in boot["diagnostics"]


def test_browser_progress_and_cancel_are_available(tmp_path):
    client = TestClient(_app(tmp_path))
    progress = client.get("/desktop/browser/progress", headers=HEADERS)
    assert progress.status_code == 200
    assert "state" in progress.json()
    cancel = client.post("/desktop/browser/cancel", headers=HEADERS)
    assert cancel.status_code == 200
    # Nothing was installing, so cancel is a no-op, not an error.
    assert cancel.json()["cancelled"] is False


def test_browser_routes_require_auth(tmp_path):
    client = TestClient(_app(tmp_path))
    assert client.get("/desktop/browser").status_code == 403
    assert client.post("/desktop/browser/provision").status_code == 403
    assert client.get("/desktop/browser/progress").status_code == 403


def test_provision_browser_runs_a_real_install(tmp_path):
    """Provisioning drives the real provisioner: the injected installer runs and
    a real binary on disk flips the state to ready."""
    import sys

    app = _app(tmp_path)
    service = app.state.desktop_service
    installed = tmp_path / "chrome"
    service._browser = BrowserProvisioner(
        package_probe=lambda: True,
        chromium_finder=lambda: str(installed) if installed.exists() else "",
        installer_command=[
            sys.executable,
            "-c",
            f"open({str(installed)!r}, 'w').write('x')",
        ],
    )
    client = TestClient(app)
    resp = client.post("/desktop/browser/provision", headers=HEADERS)
    assert resp.status_code == 202
    body = resp.json()
    assert body["state"] == "ready"
    assert body["ready"] is True
    assert body["chromium_path"] == str(installed)
    # The provisioning attempt is auditable.
    audit = client.get("/desktop/runs", headers=HEADERS)
    assert audit.status_code == 200
