"""Generated-document surface: a real file a run produced, listed and opened.

These prove the product's document loop end to end on the desktop surface: the
agent runs a real tool that writes a real ``.pptx`` into its workspace, the
write pauses for approval and only happens once approved, the file then appears
in the run's artifact listing with its true size, and "open" hands that exact
sandbox-resolved file to the operating system — refusing any path that escapes
the workspace. Only the LLM and the OS launcher are faked; the platform, policy
engine, approval store, tool and filesystem sandbox are the real ones.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

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


def _presentation_responder():  # type: ignore[no-untyped-def]
    return scripted_responder(
        plan=plan_json(("أنشئ عرضًا", "create_presentation")),
        tool_calls=[
            (
                "create_presentation",
                {
                    "path": "deck.pptx",
                    "title": "تقرير",
                    "blocks": [{"type": "paragraph", "text": "محتوى"}],
                },
            ),
            ("finish", {"result": "تم", "verified": True}),
        ],
        verification=verify_json(True, "تم"),
    )


def _app(tmp_path: Path):  # type: ignore[no-untyped-def]
    registry = ProviderRegistry()
    registry.register(FakeProvider(_presentation_responder()))
    return build_local_control_app(
        _settings(tmp_path), provider_registry=registry, default_model="fake/model"
    )


def _run_to_completion(client: TestClient, workspace: Path) -> str:
    """Start a presentation run, approve the write, return the finished run id."""
    started = client.post(
        "/desktop/runs",
        json={"goal": "أنشئ عرضًا", "workspace_dir": str(workspace)},
        headers=HEADERS,
    )
    assert started.status_code == 201
    run = started.json()
    assert run["status"] == "waiting_approval"
    assert run["pending_approval_id"]
    # The file must not exist before approval.
    assert not (workspace / "deck.pptx").exists()

    decided = client.post(
        f"/desktop/approvals/{run['pending_approval_id']}",
        json={"approve": True},
        headers=HEADERS,
    )
    assert decided.status_code == 200
    finished = decided.json()
    assert finished["status"] == "completed"
    return str(finished["id"])


@pytest.mark.skipif(
    not __import__("importlib.util", fromlist=["util"]).find_spec("pptx"),
    reason="python-pptx not installed",
)
def test_run_artifacts_lists_a_real_generated_presentation(tmp_path):
    client = TestClient(_app(tmp_path))
    workspace = tmp_path / "ws"
    run_id = _run_to_completion(client, workspace)

    # The write really happened, and it is a real OOXML package.
    produced = workspace / "deck.pptx"
    assert produced.read_bytes()[:2] == b"PK"

    listing = client.get(f"/desktop/runs/{run_id}/artifacts", headers=HEADERS).json()
    assert listing["count"] == 1
    artifact = listing["artifacts"][0]
    assert artifact["name"] == "deck.pptx"
    assert artifact["format"] == "pptx"
    assert artifact["openable"] is True
    assert artifact["run_id"] == run_id
    assert artifact["bytes"] == produced.stat().st_size > 0

    # The cross-run listing and bootstrap both surface it too.
    assert client.get("/desktop/artifacts", headers=HEADERS).json()["count"] == 1
    assert "artifacts" in client.get("/desktop/bootstrap", headers=HEADERS).json()


@pytest.mark.skipif(
    not __import__("importlib.util", fromlist=["util"]).find_spec("pptx"),
    reason="python-pptx not installed",
)
def test_open_artifact_launches_the_sandbox_resolved_file(tmp_path, monkeypatch):
    client = TestClient(_app(tmp_path))
    workspace = tmp_path / "ws"
    run_id = _run_to_completion(client, workspace)

    launched: list[list[str]] = []

    import subprocess

    def fake_popen(args, **kwargs):  # type: ignore[no-untyped-def]
        launched.append(list(args))
        return object()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    resp = client.post(
        f"/desktop/runs/{run_id}/artifacts/open",
        json={"path": "deck.pptx"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["opened"] is True
    assert launched and launched[0][-1].endswith("deck.pptx")


def test_open_artifact_refuses_a_path_outside_the_workspace(tmp_path):
    client = TestClient(_app(tmp_path))
    workspace = tmp_path / "ws"
    run_id = _run_to_completion(client, workspace)

    resp = client.post(
        f"/desktop/runs/{run_id}/artifacts/open",
        json={"path": "../../etc/passwd"},
        headers=HEADERS,
    )
    # The sandbox refuses the escape before the OS is ever asked.
    assert resp.status_code in (403, 404)
    assert client.get(f"/desktop/runs/{run_id}/artifacts", headers=HEADERS).json()["count"] == 1


def test_artifacts_are_empty_for_a_run_that_wrote_nothing(tmp_path):
    registry = ProviderRegistry()
    registry.register(FakeProvider(lambda req, n: text_response("تم")))
    app = build_local_control_app(
        _settings(tmp_path), provider_registry=registry, default_model="fake/model"
    )
    client = TestClient(app)
    started = client.post(
        "/desktop/runs",
        json={"goal": "قل مرحبا", "workspace_dir": str(tmp_path / "ws")},
        headers=HEADERS,
    )
    run_id = started.json()["id"]
    listing = client.get(f"/desktop/runs/{run_id}/artifacts", headers=HEADERS).json()
    assert listing == {"run_id": run_id, "artifacts": [], "count": 0}
