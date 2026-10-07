"""Browser provisioning.

Autonomous browsing needs a real Chromium. On a developer machine that is often
already present, but a packaged Windows install starts with neither the
Playwright package nor its browser. Rather than failing on first use with a
cryptic error, the product exposes a *provisioning* surface that reports an
honest state and can install what is missing:

    NOT_INSTALLED → INSTALLING → READY
                              ↘ BROKEN

* :func:`playwright_package_available` — is the ``playwright`` package importable?
* :func:`find_playwright_chromium` — is a Playwright-managed Chromium on disk?
* :class:`BrowserProvisioner` — reports the state and runs the real
  ``playwright install chromium`` when asked, updating state and never claiming
  ``READY`` unless a Chromium executable is actually found afterwards.

The installer is invoked as a fixed module (``python -m playwright install
chromium``) through a structured argv, never a shell, so nothing about it is
injectable. State is process-local and guarded by a lock, so two concurrent
provision requests cannot race the same install.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import shutil
import sys
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ..core.process import process_group_kwargs, terminate_process

logger = logging.getLogger(__name__)

#: Environment variable Playwright uses to relocate its browser cache.
BROWSERS_PATH_ENV = "PLAYWRIGHT_BROWSERS_PATH"


class BrowserState(StrEnum):
    """The provisioning state of the browser runtime."""

    UNKNOWN = "unknown"
    NOT_INSTALLED = "not_installed"
    INSTALLING = "installing"
    READY = "ready"
    BROKEN = "broken"


class BrowserProvisionStatus(BaseModel):
    """An honest snapshot of the browser runtime's readiness."""

    model_config = ConfigDict(frozen=True)

    state: BrowserState
    package_available: bool
    chromium_path: str = ""
    detail: str = ""
    #: Tail of the installer output while/after provisioning (bounded).
    log_tail: str = ""

    @property
    def ready(self) -> bool:
        return self.state is BrowserState.READY


def playwright_package_available() -> bool:
    """Whether the ``playwright`` package can be imported."""
    try:
        return importlib.util.find_spec("playwright") is not None
    except (ImportError, ValueError):
        return False


def playwright_browsers_path() -> Path:
    """The directory Playwright stores downloaded browsers in."""
    override = os.environ.get(BROWSERS_PATH_ENV)
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")) / "ms-playwright"


#: Relative locations of a Chromium binary inside a Playwright browser folder,
#: by platform. Playwright names the folder ``chromium-<build>``.
_CHROMIUM_RELATIVE = {
    "win": ("chrome-win", "chrome.exe"),
    "mac": ("chrome-mac", "Chromium.app", "Contents", "MacOS", "Chromium"),
    "linux": ("chrome-linux", "chrome"),
}


def find_playwright_chromium() -> str:
    """Return the path of a Playwright-managed Chromium, or ``""``.

    Only a real on-disk binary is returned; a directory that merely exists is
    not enough, so ``READY`` is never reported for an interrupted download.
    """
    root = playwright_browsers_path()
    if not root.is_dir():
        return ""
    if sys.platform.startswith("win"):
        key = "win"
    elif sys.platform == "darwin":
        key = "mac"
    else:
        key = "linux"
    relative = _CHROMIUM_RELATIVE[key]
    for folder in sorted(root.glob("chromium-*"), reverse=True):
        candidate = folder.joinpath(*relative)
        if candidate.is_file():
            return str(candidate)
    return ""


def find_system_chromium() -> str:
    """Return a system-installed Chromium/Chrome/Edge executable, or ``""``."""
    candidates = (
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
        "chrome",
        "msedge",
    )
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    return ""


class BrowserProvisioner:
    """Reports browser readiness and can install the missing pieces.

    A ``probe`` and ``installer`` can be injected so the whole state machine is
    exercised in tests without Playwright, a network, or a real install.
    """

    def __init__(
        self,
        *,
        package_probe: Callable[[], bool] = playwright_package_available,
        chromium_finder: Callable[[], str] | None = None,
        installer_command: list[str] | None = None,
        timeout_seconds: float = 900.0,
    ) -> None:
        self._package_probe = package_probe
        self._chromium_finder = chromium_finder or self._default_finder
        self._installer_command = installer_command or [
            sys.executable,
            "-m",
            "playwright",
            "install",
            "chromium",
        ]
        self._timeout = timeout_seconds
        self._state = BrowserState.UNKNOWN
        self._log_tail = ""
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _default_finder() -> str:
        return find_playwright_chromium() or find_system_chromium()

    @property
    def state(self) -> BrowserState:
        return self._state

    def status(self) -> BrowserProvisionStatus:
        """Probe the real state and return it (updating the cached state)."""
        package = self._package_probe()
        chromium = self._chromium_finder() if package else ""
        if self._state is BrowserState.INSTALLING:
            return BrowserProvisionStatus(
                state=self._state,
                package_available=package,
                chromium_path=chromium,
                detail="installation in progress",
                log_tail=self._log_tail,
            )
        if chromium:
            self._state = BrowserState.READY
            detail = "Chromium is available for browsing"
        elif self._state is BrowserState.BROKEN:
            # A failed install stays broken until a binary appears or a new
            # provision is attempted; re-deriving NOT_INSTALLED would hide the
            # failure the user just saw.
            return BrowserProvisionStatus(
                state=self._state,
                package_available=package,
                chromium_path=chromium,
                detail="the previous provisioning attempt failed",
                log_tail=self._log_tail,
            )
        elif not package:
            self._state = BrowserState.NOT_INSTALLED
            detail = (
                "the Playwright package is not installed; install the 'browser' extra "
                "and provision Chromium"
            )
        else:
            self._state = BrowserState.NOT_INSTALLED
            detail = "the Playwright package is installed but no Chromium binary was found"
        return BrowserProvisionStatus(
            state=self._state,
            package_available=package,
            chromium_path=chromium,
            detail=detail,
            log_tail=self._log_tail,
        )

    async def provision(self) -> BrowserProvisionStatus:
        """Run the real installer, then re-probe and report the resulting state."""
        async with self._lock:
            if not self._package_probe():
                self._state = BrowserState.NOT_INSTALLED
                self._log_tail = "the Playwright package is not installed"
                return self.status()
            self._state = BrowserState.INSTALLING
            self._log_tail = ""
            try:
                await self._run_installer()
            except (OSError, TimeoutError) as exc:
                self._state = BrowserState.BROKEN
                self._log_tail = f"{self._log_tail}\n{exc}".strip()
                logger.warning("browser provisioning failed: %s", exc)
                return self.status()
            finally:
                self._process = None
            # Re-probe; only a found binary yields READY.
            probe = self.status()
            if not probe.ready:
                self._state = BrowserState.BROKEN
                return self.status()
            return probe

    async def _run_installer(self) -> None:
        process = await asyncio.create_subprocess_exec(
            *self._installer_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **process_group_kwargs(),
        )
        self._process = process
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=self._timeout)
        except TimeoutError:
            await terminate_process(process)
            raise
        self._log_tail = output.decode("utf-8", errors="replace")[-2000:]
        if process.returncode != 0:
            raise OSError(f"playwright install exited with code {process.returncode}")

    async def cancel(self) -> bool:
        """Stop an in-progress install; returns whether one was running."""
        process = self._process
        if process is None:
            return False
        await terminate_process(process)
        self._state = BrowserState.BROKEN
        self._log_tail = f"{self._log_tail}\ninstallation cancelled".strip()
        return True


__all__ = [
    "BROWSERS_PATH_ENV",
    "BrowserProvisionStatus",
    "BrowserProvisioner",
    "BrowserState",
    "find_playwright_chromium",
    "find_system_chromium",
    "playwright_browsers_path",
    "playwright_package_available",
]
