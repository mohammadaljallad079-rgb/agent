"""Desktop shell: the native-webview window the user sees.

Technology decision (see ``ARCHITECTURE.md`` §"Desktop technology"): a
native-webview shell in pure Python (pywebview), not Electron and not a bundled
Chromium. It reuses the operating system's own web engine — WebView2 on Windows,
WebKit on macOS/Linux — so the bundle stays small, there is no second browser to
patch, and Windows support comes from a component the OS already maintains. It is
the same shape as a Tauri shell without adding a Rust toolchain to the build.

Security boundary: the UI **never** touches the operating system. It calls a
Python bridge (:class:`DesktopBridge`) that forwards each request to the local
runtime's authenticated loopback API. The control token lives only in this
process; it is never written into the page, so a compromised page cannot
recover it. Rendered agent/browser/email content is untrusted and is inserted as
text, never as markup.

The bridge is ordinary Python and is unit-tested without a display; only
:func:`launch_shell` needs pywebview and a web engine.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import httpx

from ..branding import WINDOW_TITLE
from ..core.resources import PACKAGE_DIR
from ..specialists.base import GENERAL
from .runtime_manager import DesktopRuntimeManager, RuntimeStartError

logger = logging.getLogger(__name__)

#: Directory holding the bundled UI (HTML/CSS/JS). Resolved relative to the
#: importable package so it is found in a source tree, an installed wheel and a
#: PyInstaller freeze alike (see :mod:`arabic_agent.core.resources`).
UI_DIR = PACKAGE_DIR / "desktop" / "ui"

#: Name of the per-user single-instance mutex. The shell acquires it and the
#: generated Inno Setup script declares it as ``AppMutex``, so the two must stay
#: in lock-step (see :mod:`arabic_agent.desktop.packaging`).
SINGLE_INSTANCE_MUTEX = "ArabicAIAgent.SingleInstance"


class DesktopBridge:
    """The JS-facing API. Holds the control token; the page never sees it.

    Every method is a thin, authenticated proxy to the runtime's ``/desktop``
    surface. It deliberately exposes *operations*, not the token or a generic
    "call any URL" method, so a page cannot escalate beyond the product API.
    """

    def __init__(self, *, base_url: str, token: str, timeout: float = 120.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"X-Local-Token": token},
            timeout=timeout,
        )

    async def _get(self, path: str) -> Any:
        response = await self._client.get(path)
        response.raise_for_status()
        return response.json()

    async def _post(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        response = await self._client.post(path, json=payload or {})
        response.raise_for_status()
        return response.json()

    async def _delete(self, path: str) -> Any:
        response = await self._client.delete(path)
        response.raise_for_status()
        return response.json()

    # -- Read surface ----------------------------------------------------
    async def bootstrap(self) -> Any:
        return await self._get("/desktop/bootstrap")

    async def list_runs(self) -> Any:
        return await self._get("/desktop/runs")

    async def get_run(self, run_id: str) -> Any:
        return await self._get(f"/desktop/runs/{run_id}")

    async def run_timeline(self, run_id: str) -> Any:
        return await self._get(f"/desktop/runs/{run_id}/timeline")

    async def list_models(self) -> Any:
        return await self._get("/desktop/models")

    async def list_approvals(self) -> Any:
        return await self._get("/desktop/approvals")

    async def modes(self) -> Any:
        return await self._get("/desktop/modes")

    async def providers(self) -> Any:
        return await self._get("/desktop/providers")

    async def test_provider(self, provider_name: str) -> Any:
        return await self._post(f"/desktop/providers/{provider_name}/test")

    async def settings(self) -> Any:
        return await self._get("/desktop/settings")

    async def permissions(self) -> Any:
        return await self._get("/desktop/permissions")

    async def diagnostics(self) -> Any:
        return await self._get("/desktop/diagnostics")

    async def export_diagnostics(self) -> Any:
        return await self._get("/desktop/diagnostics/export")

    async def version(self) -> Any:
        return await self._get("/desktop/version")

    # -- Write surface ---------------------------------------------------
    async def start_run(
        self,
        goal: str,
        workspace_dir: str | None = None,
        specialist: str = GENERAL,
    ) -> Any:
        return await self._post(
            "/desktop/runs",
            {"goal": goal, "workspace_dir": workspace_dir, "specialist": specialist},
        )

    async def cancel_run(self, run_id: str) -> Any:
        return await self._post(f"/desktop/runs/{run_id}/cancel")

    async def decide_approval(
        self, approval_id: str, approve: bool, reason: str | None = None
    ) -> Any:
        return await self._post(
            f"/desktop/approvals/{approval_id}", {"approve": approve, "reason": reason}
        )

    async def install_model(self, model_id: str) -> Any:
        return await self._post(f"/desktop/models/{model_id}/install")

    async def model_progress(self, model_id: str) -> Any:
        return await self._get(f"/desktop/models/{model_id}/progress")

    async def cancel_model_install(self, model_id: str) -> Any:
        return await self._post(f"/desktop/models/{model_id}/cancel")

    async def verify_model(self, model_id: str) -> Any:
        return await self._post(f"/desktop/models/{model_id}/verify")

    async def model_health(self, model_id: str) -> Any:
        return await self._post(f"/desktop/models/{model_id}/health")

    async def model_readiness(self) -> Any:
        return await self._get("/desktop/models/readiness")

    async def test_model_inference(self, model_id: str | None = None) -> Any:
        return await self._post("/desktop/models/test-inference", {"model_id": model_id})

    async def activate_model(self, model_id: str) -> Any:
        return await self._post(f"/desktop/models/{model_id}/activate")

    async def deactivate_model(self) -> Any:
        return await self._post("/desktop/models/active/deactivate")

    async def setup_model(self, model_id: str | None = None) -> Any:
        return await self._post("/desktop/models/setup", {"model_id": model_id})

    async def tools(self) -> Any:
        return await self._get("/desktop/tools")

    async def specialists(self) -> Any:
        return await self._get("/desktop/specialists")

    async def artifacts(self) -> Any:
        return await self._get("/desktop/artifacts")

    async def run_artifacts(self, run_id: str) -> Any:
        return await self._get(f"/desktop/runs/{run_id}/artifacts")

    async def open_artifact(self, run_id: str, path: str) -> Any:
        return await self._post(f"/desktop/runs/{run_id}/artifacts/open", {"path": path})

    async def workflows(self) -> Any:
        return await self._get("/desktop/workflows")

    async def connectors(self) -> Any:
        return await self._get("/desktop/connectors")

    async def computer(self) -> Any:
        return await self._get("/desktop/computer")

    async def printing(self) -> Any:
        return await self._get("/desktop/printing")

    async def print_jobs(self) -> Any:
        return await self._get("/desktop/printing/jobs")

    async def voice(self) -> Any:
        return await self._get("/desktop/voice")

    async def voice_devices(self) -> Any:
        return await self._get("/desktop/voice/devices")

    async def voice_models(self) -> Any:
        return await self._get("/desktop/voice/models")

    async def install_voice_model(self, model_id: str) -> Any:
        return await self._post(f"/desktop/voice/models/{model_id}/install")

    async def remove_voice_model(self, model_id: str) -> Any:
        return await self._delete(f"/desktop/voice/models/{model_id}")

    async def voice_actions(self, run_id: str | None = None) -> Any:
        path = "/desktop/voice/actions" + (f"?run_id={run_id}" if run_id else "")
        return await self._get(path)

    async def voice_start(self, goal: str, workspace_dir: str | None = None) -> Any:
        return await self._post(
            "/desktop/voice/start", {"goal": goal, "workspace_dir": workspace_dir}
        )

    async def voice_transcribe(
        self,
        run_id: str | None = None,
        seconds: float = 5.0,
        device_id: str = "",
        language: str = "ar",
    ) -> Any:
        return await self._post(
            "/desktop/voice/transcribe",
            {
                "run_id": run_id,
                "seconds": seconds,
                "device_id": device_id,
                "language": language,
            },
        )

    async def voice_synthesize(
        self,
        text: str,
        run_id: str | None = None,
        voice: str = "",
        language: str = "ar",
    ) -> Any:
        return await self._post(
            "/desktop/voice/synthesize",
            {"run_id": run_id, "text": text, "voice": voice, "language": language},
        )

    async def voice_speak(
        self,
        text: str,
        run_id: str | None = None,
        voice: str = "",
        language: str = "ar",
        device_id: str = "",
    ) -> Any:
        return await self._post(
            "/desktop/voice/speak",
            {
                "run_id": run_id,
                "text": text,
                "voice": voice,
                "language": language,
                "device_id": device_id,
            },
        )

    async def voice_play(
        self, path: str, run_id: str | None = None, device_id: str = "", language: str = "ar"
    ) -> Any:
        return await self._post(
            "/desktop/voice/play",
            {"run_id": run_id, "path": path, "device_id": device_id, "language": language},
        )

    async def voice_stop(self, op: str | None = None) -> Any:
        return await self._post("/desktop/voice/stop", {"op": op})

    async def memory(self) -> Any:
        return await self._get("/desktop/memory")

    async def remove_model(self, model_id: str) -> Any:
        return await self._delete(f"/desktop/models/{model_id}")

    async def set_mode(self, mode: str) -> Any:
        return await self._post("/desktop/modes", {"mode": mode})

    # -- Browser provisioning -------------------------------------------
    async def browser(self) -> Any:
        return await self._get("/desktop/browser")

    async def provision_browser(self) -> Any:
        return await self._post("/desktop/browser/provision")

    async def browser_progress(self) -> Any:
        return await self._get("/desktop/browser/progress")

    async def cancel_browser_provision(self) -> Any:
        return await self._post("/desktop/browser/cancel")

    async def aclose(self) -> None:
        await self._client.aclose()


def _load_index_html() -> str:
    index = UI_DIR / "index.html"
    if not index.is_file():
        raise RuntimeStartError(f"desktop UI assets are missing: {index}")
    return index.read_text(encoding="utf-8")


def _single_instance_guard() -> Any | None:
    """Hold a per-user single-instance lock for the life of the shell.

    Returns the held lock handle, or ``None`` where the platform offers no such
    primitive (every non-Windows host, so tests and Linux are unaffected).

    This is a correctness guard, not a nicety: if a second copy of the app is
    started while the first is still running, two runtimes race for the same
    per-user database and — on Windows — the second copy keeps the installed
    ``ArabicAIAgent.exe`` and ``_internal`` files open, so the next installer
    run fails with "Access is denied" when it tries to overwrite them. The
    installer also declares the same name as an ``AppMutex``, so it waits for
    this lock to clear instead of failing.

    Implemented with ``ctypes`` (stdlib) rather than ``pywin32`` so the guard
    works in the frozen payload without an extra dependency.
    """
    if not sys.platform.startswith("win"):  # pragma: no cover - POSIX
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
        if not handle:
            return None
        # ERROR_ALREADY_EXISTS (183): another instance already holds the mutex.
        if ctypes.get_last_error() == 183:
            return None
        return handle
    except (OSError, AttributeError, ImportError):  # pragma: no cover - defensive
        return None


def launch_shell(
    *,
    manager: DesktopRuntimeManager | None = None,
    title: str = WINDOW_TITLE,
) -> int:
    """Start the runtime, open the native window, and block until it closes.

    Returns a process exit code. Raises :class:`RuntimeStartError` if the runtime
    cannot start or pywebview/web engine is unavailable — the caller reports the
    reason rather than opening a broken window.
    """
    try:
        import webview  # pywebview
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeStartError(
            "the desktop shell needs pywebview (pip install pywebview) and a system web engine"
        ) from exc

    import asyncio

    _single_instance = _single_instance_guard()
    runtime_manager = manager or DesktopRuntimeManager()
    try:
        handle = asyncio.run(runtime_manager.start())
    except RuntimeStartError:
        raise
    bridge = DesktopBridge(base_url=handle.base_url, token=handle.token)

    # pywebview's js_api exposes the bridge's coroutine methods to the page as
    # `window.pywebview.api.<name>()`. The token never crosses into the DOM.
    # pywebview keeps its own reference to the window, so no local is needed.
    webview.create_window(title, html=_load_index_html(), js_api=bridge, width=1200, height=800)
    try:
        webview.start(debug=False)
    finally:
        asyncio.run(bridge.aclose())
        asyncio.run(runtime_manager.stop())
    return 0


def main() -> None:
    """Console entry point for the desktop shell."""
    try:
        code = launch_shell()
    except RuntimeStartError as exc:
        print(f"desktop shell error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    raise SystemExit(code)


__all__ = ["SINGLE_INSTANCE_MUTEX", "UI_DIR", "DesktopBridge", "launch_shell", "main"]
