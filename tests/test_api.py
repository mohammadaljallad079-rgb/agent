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


def test_metrics_endpoint_exposes_prometheus_text(settings):
    responder = scripted_responder(
        plan=plan_json(("echo", "echo")),
        tool_calls=[("echo", {"text": "hi"}), ("finish", {"result": "تم"})],
        verification=verify_json(True, "تم"),
    )
    with _client(settings, responder) as client:
        client.post("/runs", json={"goal": "قل مرحبا"})
        response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    body = response.text
    assert "aap_runs_started_total" in body
    assert "aap_runs_finished_total" in body
    assert "aap_tool_calls_total" in body
    assert "aap_model_calls_total" in body


def test_readyz_reports_probes(settings):
    responder = scripted_responder(plan=plan_json(("echo", "echo")), tool_calls=[])
    with _client(settings, responder) as client:
        response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    names = {c["name"] for c in body["checks"]}
    # The in-memory local runtime has a model route and an audit sink but no
    # Postgres/Redis, so those probes are absent rather than falsely green.
    assert names == {"model", "audit"}


def test_readyz_returns_503_when_critical_dependency_down(settings):
    from arabic_agent.container import Platform
    from arabic_agent.observability.readiness import ProbeResult, ReadinessProbe, ReadinessService

    class _Down(ReadinessProbe):
        name = "database"
        critical = True

        async def check(self) -> ProbeResult:
            return self._result(False, "query failed", started=0.0)

    responder = scripted_responder(plan=plan_json(("echo", "echo")), tool_calls=[])
    registry = ProviderRegistry()
    registry.register(FakeProvider(responder))
    platform = Platform.build(settings, provider_registry=registry, default_model="fake/model")
    platform.readiness = ReadinessService([_Down()])
    app = create_app(platform, settings=settings)
    with TestClient(app) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["ready"] is False


def test_request_id_is_echoed_and_generated(settings):
    responder = scripted_responder(plan=plan_json(("echo", "echo")), tool_calls=[])
    with _client(settings, responder) as client:
        generated = client.get("/health")
        assert generated.headers["X-Request-ID"].startswith("req_")
        supplied = client.get("/health", headers={"X-Request-ID": "req_custom_123"})
        assert supplied.headers["X-Request-ID"] == "req_custom_123"


def test_metrics_endpoint_can_be_disabled(settings):
    from arabic_agent.container import Platform

    disabled = settings.model_copy(update={"metrics_enabled": False})
    responder = scripted_responder(plan=plan_json(("echo", "echo")), tool_calls=[])
    registry = ProviderRegistry()
    registry.register(FakeProvider(responder))
    platform = Platform.build(disabled, provider_registry=registry, default_model="fake/model")
    app = create_app(platform, settings=disabled)
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 404
        # Liveness must stay up even with metrics off.
        assert client.get("/health").status_code == 200


def _workflow_body(publish: bool = True) -> dict:
    return {
        "name": "invoices",
        "description": "أرسل الفواتير",
        "publish": publish,
        "steps": [
            {
                "id": "prep",
                "kind": "deterministic",
                "action": "set",
                "arguments": {"values": [1, 2]},
            },
            {
                "id": "count",
                "kind": "deterministic",
                "action": "sum",
                "depends_on": ["prep"],
                "arguments": {"values": "{{ steps.prep.output.values }}"},
            },
            {
                "id": "confirm",
                "kind": "approval",
                "depends_on": ["count"],
                "prompt": "هل تريد الإرسال؟",
            },
        ],
    }


def test_workflow_crud_and_run_over_http(settings):
    responder = scripted_responder(plan=plan_json(("echo", "echo")), tool_calls=[])
    with _client(settings, responder) as client:
        created = client.post("/workflows", json=_workflow_body(publish=True))
        assert created.status_code == 201
        workflow = created.json()
        assert workflow["status"] == "published"
        assert workflow["latest_version"] == 1

        listed = client.get("/workflows")
        assert listed.status_code == 200
        assert any(w["id"] == workflow["id"] for w in listed.json())

        versions = client.get(f"/workflows/{workflow['id']}/versions")
        assert versions.status_code == 200
        assert versions.json()[0]["version"] == 1

        started = client.post(f"/workflows/{workflow['id']}/runs", json={})
        assert started.status_code == 201
        run = started.json()
        assert run["status"] == "waiting_approval"
        assert run["pending_approval_id"]

        steps = client.get(f"/workflow-runs/{run['id']}/steps")
        assert steps.status_code == 200
        by_id = {s["step_id"]: s for s in steps.json()}
        assert by_id["count"]["output"]["total"] == 3
        assert by_id["confirm"]["status"] == "waiting_approval"

        decided = client.post(
            f"/approvals/{run['pending_approval_id']}/decide", json={"approve": True}
        )
        assert decided.status_code == 200
        assert decided.json()["status"] == "completed"

        final = client.get(f"/workflow-runs/{run['id']}").json()
        assert final["status"] == "completed"


def test_workflow_rejects_invalid_graph_over_http(settings):
    responder = scripted_responder(plan=plan_json(("echo", "echo")), tool_calls=[])
    body = {
        "name": "bad",
        "publish": True,
        "steps": [
            {"id": "a", "kind": "deterministic", "action": "set", "depends_on": ["b"]},
            {"id": "b", "kind": "deterministic", "action": "set", "depends_on": ["a"]},
        ],
    }
    with _client(settings, responder) as client:
        response = client.post("/workflows", json=body)
    assert response.status_code == 422


def test_workflow_run_404_for_other_tenant(settings):
    from pydantic import SecretStr

    from arabic_agent.container import Platform

    secure = settings.model_copy(
        update={
            "require_auth": True,
            "api_keys": SecretStr(
                '{"k1": {"tenant_id": "tnt_1", "user_id": "usr_1"},'
                ' "k2": {"tenant_id": "tnt_2", "user_id": "usr_2"}}'
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
        h1 = {"X-API-Key": "k1"}
        h2 = {"X-API-Key": "k2"}
        workflow = client.post("/workflows", json=_workflow_body(publish=True), headers=h1).json()
        run = client.post(f"/workflows/{workflow['id']}/runs", json={}, headers=h1).json()
        assert client.get(f"/workflow-runs/{run['id']}", headers=h1).status_code == 200
        assert client.get(f"/workflow-runs/{run['id']}", headers=h2).status_code == 404
        assert client.get(f"/workflows/{workflow['id']}", headers=h2).status_code == 404


# ---------------------------------------------------------------------------
# Trigger API
# ---------------------------------------------------------------------------
def test_trigger_crud_and_manual_fire_over_http(settings):
    responder = scripted_responder(plan=plan_json(("echo", "echo")), tool_calls=[])
    with _client(settings, responder) as client:
        workflow = client.post("/workflows", json=_workflow_body(publish=True)).json()

        created = client.post(
            "/triggers",
            json={
                "name": "nightly",
                "workflow_id": workflow["id"],
                "kind": "cron",
                "cron": "0 3 * * *",
                "timezone": "Asia/Riyadh",
            },
        )
        assert created.status_code == 201
        trigger = created.json()["trigger"]
        assert trigger["kind"] == "cron"
        assert trigger["next_fire_at"] is not None
        # A cron trigger has no secret.
        assert created.json()["webhook_secret"] is None

        listed = client.get("/triggers").json()
        assert [t["id"] for t in listed] == [trigger["id"]]

        updated = client.patch(
            f"/triggers/{trigger['id']}", json={"misfire_policy": "run_one"}
        ).json()
        assert updated["misfire_policy"] == "run_one"

        fired = client.post(f"/triggers/{trigger['id']}/fire", json={"inputs": {"x": 1}})
        assert fired.status_code == 200
        run = fired.json()
        assert run["workflow_id"] == workflow["id"]

        firings = client.get(f"/triggers/{trigger['id']}/firings").json()
        assert len(firings) == 1
        assert firings[0]["workflow_run_id"] == run["id"]

        assert client.post(f"/triggers/{trigger['id']}/disable").json()["enabled"] is False
        assert client.post(f"/triggers/{trigger['id']}/enable").json()["enabled"] is True

        deleted = client.delete(f"/triggers/{trigger['id']}")
        assert deleted.status_code == 204
        assert client.get(f"/triggers/{trigger['id']}").status_code == 404


def test_trigger_webhook_secret_is_returned_once(settings):
    responder = scripted_responder(plan=plan_json(("echo", "echo")), tool_calls=[])
    with _client(settings, responder) as client:
        workflow = client.post("/workflows", json=_workflow_body(publish=True)).json()
        created = client.post(
            "/triggers",
            json={"name": "hook", "workflow_id": workflow["id"], "kind": "webhook"},
        ).json()
        assert created["webhook_secret"]
        assert created["trigger"]["webhook_id"]
        # A read never exposes the secret.
        fetched = client.get(f"/triggers/{created['trigger']['id']}").json()
        assert "webhook_secret" not in fetched


def test_webhook_endpoint_requires_signature(settings):
    responder = scripted_responder(plan=plan_json(("echo", "echo")), tool_calls=[])
    with _client(settings, responder) as client:
        workflow = client.post("/workflows", json=_workflow_body(publish=True)).json()
        created = client.post(
            "/triggers",
            json={"name": "hook", "workflow_id": workflow["id"], "kind": "webhook"},
        ).json()
        webhook_id = created["trigger"]["webhook_id"]
        # No signature headers → rejected.
        assert client.post(f"/webhooks/{webhook_id}", content=b"{}").status_code == 403

        import hashlib
        import hmac
        import time

        ts = str(int(time.time()))
        body = b'{"event":"x"}'
        secret = created["webhook_secret"]
        sig = (
            "sha256="
            + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        )
        ok = client.post(
            f"/webhooks/{webhook_id}",
            content=body,
            headers={"X-Signature": sig, "X-Timestamp": ts, "X-Delivery-ID": "d1"},
        )
        assert ok.status_code == 200
        assert ok.json()["workflow_id"] == workflow["id"]
        # Unknown webhook id → 404.
        assert (
            client.post(
                "/webhooks/whk_missing",
                content=body,
                headers={"X-Signature": sig, "X-Timestamp": ts},
            ).status_code
            == 404
        )


def test_event_endpoint_fires_matching_trigger(settings):
    responder = scripted_responder(plan=plan_json(("echo", "echo")), tool_calls=[])
    with _client(settings, responder) as client:
        workflow = client.post("/workflows", json=_workflow_body(publish=True)).json()
        client.post(
            "/triggers",
            json={
                "name": "on-invoice",
                "workflow_id": workflow["id"],
                "kind": "event",
                "event_type": "invoice.created",
            },
        )
        fired = client.post(
            "/events",
            json={"event_type": "invoice.created", "payload": {"invoice_id": "inv_1"}},
        )
        assert fired.status_code == 200
        runs = fired.json()
        assert len(runs) == 1
        assert runs[0]["workflow_id"] == workflow["id"]
        # A different event type fires nothing.
        assert client.post("/events", json={"event_type": "nope"}).json() == []


def test_trigger_kind_validation_over_http(settings):
    responder = scripted_responder(plan=plan_json(("echo", "echo")), tool_calls=[])
    with _client(settings, responder) as client:
        workflow = client.post("/workflows", json=_workflow_body(publish=True)).json()
        # A cron trigger without a schedule is a 422.
        bad = client.post(
            "/triggers",
            json={"name": "bad", "workflow_id": workflow["id"], "kind": "cron"},
        )
        assert bad.status_code == 422
        # An unknown kind is rejected by the schema.
        unknown = client.post(
            "/triggers",
            json={"name": "bad", "workflow_id": workflow["id"], "kind": "telepathy"},
        )
        assert unknown.status_code == 422
