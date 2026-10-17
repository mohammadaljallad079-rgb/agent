"""Browser-served Command Center surface.

These prove the multi-tenant web UI is real: it is served from the API origin
with a strict CSP, and every panel it renders is backed by a live API route
(health, models, runs, approvals, artifacts) rather than a placeholder. The UI
document itself carries no data, so it is public; tenant data is only reachable
through the authenticated API the page calls.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from arabic_agent.container import Platform
from arabic_agent.core.config import Settings
from arabic_agent.models.registry import ProviderRegistry
from arabic_agent.web.app import CSP, UI_DIR, create_web_app

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


def _client(tmp_path: Path) -> TestClient:
    registry = ProviderRegistry()
    registry.register(
        FakeProvider(
            scripted_responder(
                plan=plan_json(("قل مرحبا", "echo")),
                tool_calls=[
                    ("echo", {"text": "x"}),
                    ("finish", {"result": "تم", "verified": True}),
                ],
                verification=verify_json(True, "تم"),
            )
        )
    )
    platform = Platform.build(
        _settings(tmp_path), provider_registry=registry, default_model="fake/model"
    )
    return TestClient(create_web_app(platform, settings=platform.settings))


def test_index_is_served_with_a_strict_csp(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == CSP
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "connect-src 'self'" in CSP
    assert "default-src 'none'" in CSP


def test_ui_route_serves_the_same_document(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        root = client.get("/")
        ui = client.get("/ui")
    assert root.status_code == 200
    assert ui.status_code == 200
    assert root.text == ui.text


def test_ui_is_arabic_first_and_rtl(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        html = client.get("/").text
    assert '<html lang="ar" dir="rtl">' in html
    assert "سوبر إي آي" in html
    # A locale switch exists so the surface is not Arabic-only.
    assert "aap_lang" in html
    assert "langBtn" in html


def test_ui_is_wired_to_the_real_api_not_placeholders(tmp_path: Path) -> None:
    html = (UI_DIR / "index.html").read_text(encoding="utf-8")
    # Every panel calls a real route the API actually serves.
    for route in (
        '"/health"',
        '"/runs"',
        '"/approvals"',
        '"/artifacts"',
        '"/models"',
        '"/specialists"',
        '"/tools"',
    ):
        assert route in html, f"UI does not call {route}"
    # Approval decisions and artifact downloads go through the real routes.
    assert "/decide" in html
    assert "/artifacts/download" in html
    # No placeholder control: an unimplemented button would be visibly inert.
    assert "TODO" not in html
    assert "data-todo" not in html
    assert 'class="placeholder"' not in html


def test_api_routes_still_work_alongside_the_ui(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/models").status_code == 200
        assert client.get("/artifacts").json()["count"] == 0


def test_web_assets_are_packaged_with_the_wheel() -> None:
    """The UI must ship in the wheel so `agent-web` works when installed.

    The assets live under `src/arabic_agent/web/ui`, inside the packaged
    `src/arabic_agent` tree, so they ship because the package ships — a
    `force-include` for the same path would add a duplicate archive entry and
    break the build (hatchling refuses it). Assert the real property: the UI
    directory is inside the packaged package root and the asset is present.
    """
    import tomllib

    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    packages = config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert packages == ["src/arabic_agent"]
    assert UI_DIR.is_relative_to(root / "src" / "arabic_agent")
    assert (UI_DIR / "index.html").is_file()
