"""An honest null browser backend.

When no automation engine is installed the platform must not pretend browsing
works. The null backend reports itself unavailable with a reason, and every
operation raises a clear :class:`ConfigurationError` rather than silently doing
nothing. It exists so the browser tools can be *registered* (and their
permissions/approvals reasoned about) on a machine without Playwright, while
failing honestly on first use.
"""

from __future__ import annotations

from ..base import (
    BrowserBackend,
    DialogInfo,
    DownloadedFile,
    PageSnapshot,
    Screenshot,
    TabInfo,
)


class NullBrowserBackend(BrowserBackend):
    """A backend that is always unavailable and says why."""

    name = "null"

    def __init__(self, reason: str = "no browser backend is available") -> None:
        self.available = False
        self.unavailable_reason = reason

    async def start(self) -> None:
        self.require_available()

    async def aclose(self) -> None:
        return None

    async def list_tabs(self) -> list[TabInfo]:
        self.require_available()
        return []

    async def open_tab(self, url: str = "") -> TabInfo:
        self.require_available()
        raise AssertionError  # pragma: no cover - unreachable

    async def switch_tab(self, tab_id: str) -> TabInfo:
        self.require_available()
        raise AssertionError  # pragma: no cover - unreachable

    async def close_tab(self, tab_id: str) -> None:
        self.require_available()

    async def active_tab(self) -> TabInfo:
        self.require_available()
        raise AssertionError  # pragma: no cover - unreachable

    async def navigate(self, url: str, *, timeout_ms: int | None = None) -> TabInfo:
        self.require_available()
        raise AssertionError  # pragma: no cover - unreachable

    async def go_back(self) -> TabInfo:
        self.require_available()
        raise AssertionError  # pragma: no cover - unreachable

    async def go_forward(self) -> TabInfo:
        self.require_available()
        raise AssertionError  # pragma: no cover - unreachable

    async def reload(self) -> TabInfo:
        self.require_available()
        raise AssertionError  # pragma: no cover - unreachable

    async def snapshot(self, *, max_elements: int = 60) -> PageSnapshot:
        self.require_available()
        raise AssertionError  # pragma: no cover - unreachable

    async def screenshot(self, *, full_page: bool = False) -> Screenshot:
        self.require_available()
        raise AssertionError  # pragma: no cover - unreachable

    async def click(self, ref: str, *, timeout_ms: int | None = None) -> None:
        self.require_available()

    async def type_text(self, ref: str, text: str, *, clear: bool = True) -> None:
        self.require_available()

    async def press_key(self, key: str, *, ref: str | None = None) -> None:
        self.require_available()

    async def select_option(self, ref: str, value: str) -> None:
        self.require_available()

    async def set_checked(self, ref: str, *, checked: bool) -> None:
        self.require_available()

    async def scroll(self, *, amount: int, ref: str | None = None) -> None:
        self.require_available()

    async def wait_for(self, *, kind: str, value: str = "", timeout_ms: int | None = None) -> None:
        self.require_available()

    async def download(self, ref: str, *, timeout_ms: int | None = None) -> DownloadedFile:
        self.require_available()
        raise AssertionError  # pragma: no cover - unreachable

    async def upload(self, ref: str, path: str) -> None:
        self.require_available()

    async def dialog(self) -> DialogInfo | None:
        self.require_available()
        return None

    async def accept_dialog(self, *, prompt_text: str | None = None) -> None:
        self.require_available()

    async def dismiss_dialog(self) -> None:
        self.require_available()


__all__ = ["NullBrowserBackend"]
