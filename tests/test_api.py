"""API surface tests using FastAPI's TestClient against a real platform."""

from __future__ import annotations

from fastapi.testclient import TestClient

from arabic_agent.api.app import create_app
from arabic_agent.api.auth import ApiKeyAuth
from arabic_agent.models.registry import ProviderRegistry

from .helpers import FakeProvider, plan_json, scripted_responder, verify_json


def _client(settings, respond) -> TestClient:
    from arabic_agent.container import Platform

    registry = ProviderRegistry()
    registry.register(FakeProvider(respond))
    platform = Platform.build(settings, provider_registry=registry, default_model="fake/model")
    app = create_app(platform, settings=settings)
    return TestClient(app)


def test_health_reports_real_state(settings):
    responder = scripted_responder(
        plan=plan_json(("echo", "echo")), tool_calls=[("echo", {"text": "x"})]
    )
    with _client(settings, responder) as client:
        response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["runtime"] == "cloud"
    assert "echo" in body["tools"]
    assert body["default_model"] == "fake/model"


def test_tools_endpoint_lists_permissions(settings):
    responder = scripted_responder(plan=plan_json(("echo", "echo")), tool_calls=[])
    with _client(settings, responder) as client:
        response = client.get("/tools")
    assert response.status_code == 200
    tools = {t["name"]: t for t in response.json()}
    assert "READ_FILE" in tools["read_file"]["permissions"]
    assert tools["run_command"]["risks"]["RUN_COMMAND"] == "high"


def test_run_endpoint_executes_and_returns_result(settings):
    responder = scripted_responder(
        plan=plan_json(("echo", "echo")),
        tool_calls=[("echo", {"text": "hi"}), ("finish", {"result": "تم"})],
        verification=verify_json(True, "تم"),
    )
    with _client(settings, responder) as client:
        response = client.post("/runs", json={"goal": "قل مرحبا"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["verified"] is True


def test_run_validation_rejects_empty_goal(settings):
    responder = scripted_responder(plan=plan_json(("echo", "echo")), tool_calls=[])
    with _client(settings, responder) as client:
        response = client.post("/runs", json={"goal": ""})
    assert response.status_code == 422


def test_auth_is_enforced_when_configured(settings):
    from pydantic import SecretStr

    from arabic_agent.container import Platform

    secure = settings.model_copy(
        update={
            "require_auth": True,
            "api_keys": SecretStr(
                '{"k1": {"tenant_id": "tnt_1", "user_id": "usr_1", "roles": ["admin"]}}'
            ),
        }
    )
    registry = ProviderRegistry()
    registry.register(
        FakeProvider(scripted_responder(plan=plan_json(("echo", "echo")), tool_calls=[]))
    )
    platform = Platform.build(secure, provider_registry=registry, default_model="fake/model")
    app = create_app(platform, settings=secure, auth=ApiKeyAuth.from_settings(secure))
    with TestClient(app) as client:
        assert client.get("/tools").status_code == 401
        assert client.get("/tools", headers={"X-API-Key": "k1"}).status_code == 200
        assert client.get("/tools", headers={"X-API-Key": "wrong"}).status_code == 401


def test_approval_flow_over_http(settings, tmp_path):
    responder = scripted_responder(
        plan=plan_json(("write a file", "write_file")),
        tool_calls=[
            ("write_file", {"path": "a.txt", "content": "hi"}),
            ("finish", {"result": "تم"}),
        ],
        verification=verify_json(True, "تم"),
    )
    with _client(settings, responder) as client:
        run = client.post("/runs", json={"goal": "اكتب ملف"}).json()
        assert run["status"] == "waiting_approval"
        approval_id = run["pending_approval_id"]

        pending = client.get("/approvals").json()
        assert any(a["id"] == approval_id for a in pending)

        decided = client.post(f"/approvals/{approval_id}/decide", json={"approve": True})
        assert decided.status_code == 200
        assert decided.json()["status"] == "completed"

    # The approved write must land inside the run workspace, never the cwd.
    written = list((tmp_path / "data" / "workspaces").rglob("a.txt"))
    assert written, "approved write did not land in the run workspace"
    assert written[0].read_text(encoding="utf-8") == "hi"
