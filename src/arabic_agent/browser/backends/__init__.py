"""Browser backend selection.

Builds the right backend for the current platform and configuration, mirroring
``computer/backends/__init__.py``. ``auto`` picks the real Playwright/Chromium
backend when it is available, and otherwise an honest null backend that reports
why. A specific ``name`` is honoured, but an unknown or unavailable one degrades
to the null backend rather than failing at import time.
"""

from __future__ import annotations

from ...core.enums import PlatformKind
from ..base import BrowserBackend
from .null import NullBrowserBackend
from .playwright import PlaywrightBrowserBackend

#: Backend ids the operator may select explicitly.
KNOWN_BACKENDS = frozenset({"auto", "playwright", "null"})


def build_browser_backend(
    *,
    backend: str = "auto",
    platform: PlatformKind = PlatformKind.LINUX,
    headless: bool = True,
    executable_path: str = "",
    download_dir: str,
    max_download_bytes: int = 64 * 1024 * 1024,
    timeout_ms: int = 15_000,
    user_agent: str = "",
) -> BrowserBackend:
    """Construct a browser backend, never raising on a missing engine."""
    name = (backend or "auto").lower()

    if name == "null":
        return NullBrowserBackend("browser backend explicitly disabled")

    if name not in KNOWN_BACKENDS:
        return NullBrowserBackend(f"unknown browser backend {backend!r}")

    # ``auto`` and ``playwright`` both try the real backend; if it reports
    # itself unavailable, return an honest null backend carrying the reason.
    candidate = PlaywrightBrowserBackend(
        headless=headless,
        executable_path=executable_path,
        download_dir=download_dir,
        max_download_bytes=max_download_bytes,
        timeout_ms=timeout_ms,
        user_agent=user_agent,
    )
    if candidate.available:
        return candidate
    if name == "playwright":
        return candidate
    return NullBrowserBackend(candidate.unavailable_reason)


__all__ = ["KNOWN_BACKENDS", "build_browser_backend"]
