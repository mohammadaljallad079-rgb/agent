"""Desktop runtime lifecycle + shell bridge tests.

The runtime manager is exercised against a real child process (a tiny stub that
serves the readiness endpoint), so process launch, environment injection, port
selection, readiness polling and shutdown are proven for real. The shell bridge
is exercised against the real desktop app running under uvicorn on a loopback
port, so its authenticated proxy calls hit the real HTTP surface.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from pydantic import SecretStr

from arabic_agent.core.config import Settings
from arabic_agent.core.enums import OperatingMode, RuntimeKind
from arabic_agent.desktop.runtime_manager import (
    DesktopRuntimeManager,
    RuntimeStartError,
    runtime_command,
)
from arabic_agent.desktop.shell import UI_DIR, DesktopBridge, _load_index_html
from arabic_agent.local.main import build_local_control_app
from arabic_agent.models.registry import ProviderRegistry

from .helpers import FakeProvider, text_response

TOKEN = "control-secret"

_STUB = """
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def do_GET(self):
        if self.path == "/local/status":
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


port = int(os.environ["AAP_LOCAL_CONTROL_PORT"])
assert os.environ["AAP_LOCAL_CONTROL_TOKEN"]
ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_runtime_command_is_module_invocation():
    assert runtime_command()[1:] == ["-m", "arabic_agent.local.main"]


def test_manager_refuses_before_start():
    manager = DesktopRuntimeManager()
    assert manager.handle is None
    assert manager.is_running() is False
    with pytest.raises(RuntimeStartError):
        _ = manager.base_url


async def test_manager_starts_and_stops_real_process(tmp_path: Path):
    script = tmp_path / "stub.py"
    script.write_text(_STUB, encoding="utf-8")
    manager = DesktopRuntimeManager(
        command=[sys.executable, str(script)],
        ready_timeout=20.0,
    )
    handle = await manager.start()
    try:
        assert manager.is_running() is True
        assert handle.base_url.startswith("http://127.0.0.1:")
        assert handle.token
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(
                f"{handle.base_url}/local/status", headers={"X-Local-Token": handle.token}
            )
        assert resp.status_code == 200
    finally:
        await manager.stop()
    assert manager.is_running() is False
    assert manager.handle is None


async def test_manager_reports_when_process_exits_immediately(tmp_path: Path):
    script = tmp_path / "die.py"
    script.write_text("raise SystemExit(3)\n", encoding="utf-8")
    manager = DesktopRuntimeManager(command=[sys.executable, str(script)], ready_timeout=5.0)
    with pytest.raises(RuntimeStartError):
        await manager.start()
    assert manager.is_running() is False


async def test_manager_restart(tmp_path: Path):
    script = tmp_path / "stub.py"
    script.write_text(_STUB, encoding="utf-8")
    manager = DesktopRuntimeManager(command=[sys.executable, str(script)], ready_timeout=20.0)
    first = await manager.start()
    second = await manager.restart()
    try:
        assert second.port != first.port or second.token != first.token
        assert manager.is_running() is True
    finally:
        await manager.stop()


# --- Shell bridge -----------------------------------------------------------
def _desktop_app(tmp_path: Path):  # type: ignore[no-untyped-def]
    settings = Settings(
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
    registry = ProviderRegistry()
    registry.register(FakeProvider(lambda req, n: text_response("تم")))
    return build_local_control_app(settings, provider_registry=registry, default_model="fake/model")


@pytest.fixture
def live_desktop(tmp_path: Path):
    """Run the real desktop app under uvicorn on a free loopback port."""
    app = _desktop_app(tmp_path)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            break
        time.sleep(0.05)
    else:  # pragma: no cover - startup failure
        raise RuntimeStartError("uvicorn did not start")
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10)


async def test_bridge_bootstrap_over_real_http(live_desktop: str):
    bridge = DesktopBridge(base_url=live_desktop, token=TOKEN)
    try:
        boot = await bridge.bootstrap()
        assert boot["modes"]["current"] == OperatingMode.OFFLINE.value
        assert boot["settings"]["general"]["environment"] == "test"
        version = await bridge.version()
        assert version["version"]
    finally:
        await bridge.aclose()


async def test_bridge_start_and_list_run(live_desktop: str):
    bridge = DesktopBridge(base_url=live_desktop, token=TOKEN)
    try:
        run = await bridge.start_run("قل مرحبا")
        assert run["id"]
        runs = await bridge.list_runs()
        assert any(r["id"] == run["id"] for r in runs)
        timeline = await bridge.run_timeline(run["id"])
        assert isinstance(timeline, list)
        approvals = await bridge.list_approvals()
        assert isinstance(approvals, list)
    finally:
        await bridge.aclose()


async def test_bridge_exposes_no_generic_url_method(live_desktop: str):
    """The bridge must not offer a way to call an arbitrary URL."""
    bridge = DesktopBridge(base_url=live_desktop, token=TOKEN)
    try:
        assert not hasattr(bridge, "get")
        assert not hasattr(bridge, "request")
    finally:
        await bridge.aclose()


async def test_bridge_rejects_bad_token(live_desktop: str):
    bridge = DesktopBridge(base_url=live_desktop, token="wrong")
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await bridge.bootstrap()
    finally:
        await bridge.aclose()


async def test_bridge_browser_surface_over_real_http(live_desktop: str):
    bridge = DesktopBridge(base_url=live_desktop, token=TOKEN)
    try:
        view = await bridge.browser()
        assert "state" in view
        progress = await bridge.browser_progress()
        assert "state" in progress
        cancelled = await bridge.cancel_browser_provision()
        assert cancelled["cancelled"] is False
    finally:
        await bridge.aclose()


def test_ui_assets_are_arabic_first_and_isolated():
    html = _load_index_html()
    assert 'dir="rtl"' in html
    assert 'lang="ar"' in html
    assert "Content-Security-Policy" in html
    assert "connect-src 'none'" in html
    assert "الوكيل العربي" in html
    # The control token must never be baked into the page.
    assert TOKEN not in html


def test_ui_has_a_real_browser_provisioning_surface():
    """The browser section is wired to the bridge, not a placeholder button."""
    html = _load_index_html()
    assert 'id="s-browser"' in html
    assert 'id="provisionBtn"' in html
    assert 'id="cancelProvisionBtn"' in html
    # The section is reachable from the navigation.
    assert '"browser"' in html
    # The handlers call the real bridge methods.
    assert "a.provision_browser()" in html
    assert "a.cancel_browser_provision()" in html
    # Arabic strings are present for the surface.
    assert "متصفح الويب" in html
    assert "تجهيز المتصفح" in html


def test_ui_has_a_real_artifacts_surface():
    """The artifacts section is wired to the bridge, not a placeholder button."""
    html = _load_index_html()
    assert 'id="s-artifacts"' in html
    assert 'id="artifacts"' in html
    # Reachable from the navigation and backed by the bridge.
    assert '"artifacts"' in html
    assert "a.artifacts()" in html
    assert "a.open_artifact(" in html
    # Arabic strings are present for the surface.
    assert "المستندات الناتجة" in html
    assert "فتح" in html


def test_ui_has_a_real_first_run_setup_surface():
    """First-run setup calls the real one-step bridge method."""
    html = _load_index_html()
    assert 'id="setupBtn"' in html
    assert "a.setup_model(" in html
    assert "إعداد النموذج" in html


def test_ui_dir_exists():
    assert UI_DIR.is_dir()
    assert (UI_DIR / "index.html").is_file()


async def test_bridge_provider_surface_over_real_http(live_desktop: str):
    bridge = DesktopBridge(base_url=live_desktop, token=TOKEN)
    try:
        view = await bridge.providers()
        assert "providers" in view
        assert "default_model" in view
        result = await bridge.test_provider("fake")
        assert result["ok"] is True
    finally:
        await bridge.aclose()


def test_ui_has_a_real_provider_surface():
    """The providers section is wired to the bridge, not a placeholder."""
    html = _load_index_html()
    assert 'id="s-providers"' in html
    assert '"providers"' in html
    assert "a.test_provider(" in html or "api().test_provider(" in html
    assert "مزوّدو الذكاء السحابي" in html
    assert "اختبار الاتصال" in html


def test_load_index_html_missing_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import arabic_agent.desktop.shell as shell

    monkeypatch.setattr(shell, "UI_DIR", tmp_path / "nope")
    with pytest.raises(RuntimeStartError):
        shell._load_index_html()


def test_launch_shell_without_pywebview_reports_clearly(monkeypatch: pytest.MonkeyPatch):
    """When pywebview is absent, the shell reports it rather than opening nothing."""
    import builtins

    import arabic_agent.desktop.shell as shell

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if name == "webview":
            raise ImportError("no webview")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeStartError):
        shell.launch_shell()
