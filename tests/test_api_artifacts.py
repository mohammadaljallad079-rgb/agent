"""Online artifact and model-status surface.

These prove the browser-served (cloud) API exposes the same real artifact loop
as the desktop surface: an agent run writes a real file into its sandboxed
workspace, the file is listed with its true size, downloaded byte-for-byte, and
a path that escapes the workspace is refused. Only the LLM is faked; the
platform, policy engine, approval store, tool, filesystem sandbox and HTTP layer
are the real ones.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from arabic_agent.api.app import create_app
from arabic_agent.container import Platform
from arabic_agent.core.config import Settings
from arabic_agent.models.registry import ProviderRegistry

from .helpers import FakeProvider, plan_json, scripted_responder, verify_json


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        require_auth=False,
        local_store_dir=str(tmp_path / "data"),
        max_steps_per_run=20,
        database_url=SecretStr(""),
        redis_url=SecretStr(""),
        execution_mode="inline",
    )


def _write_responder():  # type: ignore[no-untyped-def]
    return scripted_responder(
        plan=plan_json(("اكتب ملف", "write_file")),
        tool_calls=[
            ("write_file", {"path": "تقرير.md", "content": "مرحبا بالعالم"}),
            ("finish", {"result": "تم", "verified": True}),
        ],
        verification=verify_json(True, "تم"),
    )


def _client(tmp_path: Path, respond) -> TestClient:  # type: ignore[no-untyped-def]
    registry = ProviderRegistry()
    registry.register(FakeProvider(respond))
    platform = Platform.build(
        _settings(tmp_path), provider_registry=registry, default_model="fake/model"
    )
    return TestClient(create_app(platform, settings=platform.settings))


def _run_to_completion(client: TestClient, workspace: Path) -> str:
    started = client.post("/runs", json={"goal": "اكتب ملف", "workspace_dir": str(workspace)})
    assert started.status_code == 200
    run = started.json()
    assert run["status"] == "waiting_approval"
    assert not (workspace / "تقرير.md").exists()

    decided = client.post(f"/approvals/{run['pending_approval_id']}/decide", json={"approve": True})
    assert decided.status_code == 200
    finished = decided.json()
    assert finished["status"] == "completed"
    return str(finished["run_id"])


def test_run_artifacts_lists_a_real_generated_file(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with _client(tmp_path, _write_responder()) as client:
        run_id = _run_to_completion(client, workspace)
        response = client.get(f"/runs/{run_id}/artifacts")
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == run_id
    assert body["count"] == 1
    artifact = body["artifacts"][0]
    assert artifact["name"] == "تقرير.md"
    assert artifact["path"] == "تقرير.md"
    assert artifact["format"] == "md"
    assert artifact["bytes"] == len("مرحبا بالعالم".encode())


def test_artifact_download_returns_the_exact_bytes(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with _client(tmp_path, _write_responder()) as client:
        run_id = _run_to_completion(client, workspace)
        response = client.get(f"/runs/{run_id}/artifacts/download", params={"path": "تقرير.md"})
    assert response.status_code == 200
    assert response.content == "مرحبا بالعالم".encode()
    assert response.headers["content-type"].startswith("text/markdown")
    # A downloaded document must never be sniffed into an executable type.
    assert response.headers["x-content-type-options"] == "nosniff"


def test_artifact_download_refuses_a_path_outside_the_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")
    with _client(tmp_path, _write_responder()) as client:
        run_id = _run_to_completion(client, workspace)
        escaped = client.get(f"/runs/{run_id}/artifacts/download", params={"path": "../secret.txt"})
        absolute = client.get(f"/runs/{run_id}/artifacts/download", params={"path": str(secret)})
    assert escaped.status_code in (403, 404)
    assert absolute.status_code in (403, 404)


def test_artifacts_are_empty_for_a_run_that_wrote_nothing(tmp_path: Path) -> None:
    responder = scripted_responder(
        plan=plan_json(("لا شيء", "echo")),
        tool_calls=[("echo", {"text": "x"}), ("finish", {"result": "تم", "verified": True})],
        verification=verify_json(True, "تم"),
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with _client(tmp_path, responder) as client:
        started = client.post("/runs", json={"goal": "قل مرحبا", "workspace_dir": str(workspace)})
        run_id = started.json()["run_id"]
        body = client.get(f"/runs/{run_id}/artifacts").json()
    assert body == {"run_id": run_id, "artifacts": [], "count": 0}


def test_artifact_listing_is_tenant_scoped(tmp_path: Path) -> None:
    """A second tenant cannot read the first tenant's run artifacts."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    keys = SecretStr(
        '{"k1": {"tenant_id": "tnt_1", "user_id": "usr_1"},'
        ' "k2": {"tenant_id": "tnt_2", "user_id": "usr_2"}}'
    )
    registry = ProviderRegistry()
    registry.register(FakeProvider(_write_responder()))
    platform = Platform.build(
        _settings(tmp_path).model_copy(update={"require_auth": True, "api_keys": keys}),
        provider_registry=registry,
        default_model="fake/model",
    )
    with TestClient(create_app(platform, settings=platform.settings)) as client:
        started = client.post(
            "/runs",
            json={"goal": "اكتب ملف", "workspace_dir": str(workspace)},
            headers={"X-API-Key": "k1"},
        )
        run_id = started.json()["run_id"]
        other = client.get(f"/runs/{run_id}/artifacts", headers={"X-API-Key": "k2"})
    assert other.status_code == 404


def test_model_status_reports_a_real_provider(tmp_path: Path) -> None:
    with _client(tmp_path, _write_responder()) as client:
        response = client.get("/models")
    assert response.status_code == 200
    body = response.json()
    assert body["default_model"] == "fake/model"
    assert [p["name"] for p in body["providers"]] == ["fake"]
    assert body["providers"][0]["usable"] is True


def test_voice_status_is_honest_on_a_cloud_build(tmp_path: Path) -> None:
    """Speech tools are local-only; a cloud build reports them unavailable."""
    with _client(tmp_path, _write_responder()) as client:
        body = client.get("/voice/status").json()
    assert body["enabled"] is False
    assert body["reason"]


def test_model_status_reports_setup_required_honestly(tmp_path: Path) -> None:
    """With no model configured the platform reports it, never a fake ready."""
    platform = Platform.build(_settings(tmp_path))
    with TestClient(create_app(platform, settings=platform.settings)) as client:
        body = client.get("/models").json()
    assert body["providers"][0]["usable"] is False
    assert body["providers"][0]["detail"]


@pytest.mark.parametrize("suffix", [".html", ".json"])
def test_download_serves_markup_as_a_non_rendering_attachment(tmp_path: Path, suffix: str) -> None:
    """A generated page is downloaded, never served as a renderable type."""
    from arabic_agent.artifacts import media_type_for

    assert media_type_for(Path(f"x{suffix}")) == "application/octet-stream"
