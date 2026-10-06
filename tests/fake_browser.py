"""Test double for the browser subsystem.

This doubles the *browser engine*, not the code under test: the URL policy,
action budget, observation store, web-content boundary, source ledger, runtime,
tools, permissions and audit all run for real against this scripted backend.
Only Chromium is replaced, so unit tests are deterministic and need no browser
binary, while the real Playwright path is covered separately by the E2E suite.

A scripted page is a :class:`PageSnapshot`; the fake serves one per tab, records
every call, and lets a test mutate what the next snapshot returns (to simulate a
page change) or open a dialog.
"""

from __future__ import annotations

from arabic_agent.browser.base import (
    BrowserBackend,
    BrowserPageState,
    DialogInfo,
    DownloadedFile,
    ElementInfo,
    PageSnapshot,
    Screenshot,
    TabInfo,
)

#: A minimal, real PNG header + a few bytes. The backend never decodes it.
FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def make_snapshot(
    *,
    url: str = "https://example.com/",
    title: str = "Example",
    text: str = "Hello world",
    headings: tuple[str, ...] = ("Welcome",),
    elements: tuple[ElementInfo, ...] = (),
    links: tuple[ElementInfo, ...] = (),
    state: BrowserPageState = BrowserPageState.READY,
    scroll_y: float = 0.0,
    scroll_height: float = 0.0,
    viewport_height: float = 720.0,
    dialog: DialogInfo | None = None,
) -> PageSnapshot:
    """Build a scripted page snapshot with sensible defaults."""
    return PageSnapshot(
        url=url,
        title=title,
        state=state,
        text=text,
        headings=headings,
        elements=elements,
        links=links,
        dialog=dialog,
        scroll_y=scroll_y,
        scroll_height=scroll_height,
        viewport_height=viewport_height,
    )


class FakeBrowserBackend(BrowserBackend):
    """A scripted browser backend that records calls."""

    name = "fake"
    available = True
    unavailable_reason = ""

    def __init__(
        self,
        *,
        snapshot: PageSnapshot | None = None,
        tabs: list[TabInfo] | None = None,
        download: DownloadedFile | None = None,
        dialog: DialogInfo | None = None,
    ) -> None:
        self._snapshot = snapshot or make_snapshot()
        self._tabs: list[TabInfo] = tabs or [
            TabInfo(id="tab1", url=self._snapshot.url, title=self._snapshot.title, active=True)
        ]
        self._active = next((t.id for t in self._tabs if t.active), self._tabs[0].id)
        self._download = download
        self._dialog = dialog
        self.calls: list[tuple[str, object]] = []
        self.started = False
        self.closed = False

    # -- Scripting helpers ----------------------------------------------
    def set_snapshot(self, snapshot: PageSnapshot) -> None:
        """Change what the next snapshot returns (simulates a page change)."""
        self._snapshot = snapshot

    def set_dialog(self, dialog: DialogInfo | None) -> None:
        self._dialog = dialog

    # -- Lifecycle -------------------------------------------------------
    async def start(self) -> None:
        self.calls.append(("start", None))
        self.started = True

    async def aclose(self) -> None:
        self.calls.append(("aclose", None))
        self.closed = True

    # -- Tabs ------------------------------------------------------------
    async def list_tabs(self) -> list[TabInfo]:
        self.calls.append(("list_tabs", None))
        return [tab.model_copy(update={"active": tab.id == self._active}) for tab in self._tabs]

    async def open_tab(self, url: str = "") -> TabInfo:
        self.calls.append(("open_tab", url))
        tab = TabInfo(
            id=f"tab{len(self._tabs) + 1}",
            url=url or self._snapshot.url,
            title=self._snapshot.title,
            active=True,
        )
        self._tabs.append(tab)
        self._active = tab.id
        return tab

    async def switch_tab(self, tab_id: str) -> TabInfo:
        self.calls.append(("switch_tab", tab_id))
        for tab in self._tabs:
            if tab.id == tab_id:
                self._active = tab_id
                return tab
        from arabic_agent.core.errors import NotFoundError

        raise NotFoundError("no such tab", details={"tab_id": tab_id})

    async def close_tab(self, tab_id: str) -> None:
        self.calls.append(("close_tab", tab_id))
        self._tabs = [t for t in self._tabs if t.id != tab_id]
        if self._active == tab_id and self._tabs:
            self._active = self._tabs[0].id

    async def active_tab(self) -> TabInfo:
        for tab in self._tabs:
            if tab.id == self._active:
                return tab.model_copy(update={"active": True})
        return TabInfo(id=self._active)

    # -- Navigation ------------------------------------------------------
    async def navigate(self, url: str, *, timeout_ms: int | None = None) -> TabInfo:
        self.calls.append(("navigate", url))
        self._snapshot = self._snapshot.model_copy(update={"url": url})
        return await self.active_tab()

    async def go_back(self) -> TabInfo:
        self.calls.append(("go_back", None))
        return await self.active_tab()

    async def go_forward(self) -> TabInfo:
        self.calls.append(("go_forward", None))
        return await self.active_tab()

    async def reload(self) -> TabInfo:
        self.calls.append(("reload", None))
        return await self.active_tab()

    # -- Observation -----------------------------------------------------
    async def snapshot(self, *, max_elements: int = 60) -> PageSnapshot:
        self.calls.append(("snapshot", max_elements))
        elements = self._snapshot.elements[:max_elements]
        return self._snapshot.model_copy(update={"elements": elements})

    async def screenshot(self, *, full_page: bool = False) -> Screenshot:
        self.calls.append(("screenshot", full_page))
        return Screenshot(data=FAKE_PNG, mime_type="image/png", width=1280, height=720)

    # -- Interaction -----------------------------------------------------
    async def click(self, ref: str, *, timeout_ms: int | None = None) -> None:
        self.calls.append(("click", ref))

    async def type_text(self, ref: str, text: str, *, clear: bool = True) -> None:
        self.calls.append(("type_text", (ref, text, clear)))

    async def press_key(self, key: str, *, ref: str | None = None) -> None:
        self.calls.append(("press_key", (key, ref)))

    async def select_option(self, ref: str, value: str) -> None:
        self.calls.append(("select_option", (ref, value)))

    async def set_checked(self, ref: str, *, checked: bool) -> None:
        self.calls.append(("set_checked", (ref, checked)))

    async def scroll(self, *, amount: int, ref: str | None = None) -> None:
        self.calls.append(("scroll", (amount, ref)))

    async def wait_for(self, *, kind: str, value: str = "", timeout_ms: int | None = None) -> None:
        self.calls.append(("wait_for", (kind, value, timeout_ms)))

    # -- Files -----------------------------------------------------------
    async def download(self, ref: str, *, timeout_ms: int | None = None) -> DownloadedFile:
        self.calls.append(("download", ref))
        if self._download is None:
            raise AssertionError("no download scripted")
        return self._download

    async def upload(self, ref: str, path: str) -> None:
        self.calls.append(("upload", (ref, path)))

    # -- Dialogs ---------------------------------------------------------
    async def dialog(self) -> DialogInfo | None:
        self.calls.append(("dialog", None))
        return self._dialog

    async def accept_dialog(self, *, prompt_text: str | None = None) -> None:
        self.calls.append(("accept_dialog", prompt_text))
        self._dialog = None

    async def dismiss_dialog(self) -> None:
        self.calls.append(("dismiss_dialog", None))
        self._dialog = None


__all__ = ["FAKE_PNG", "FakeBrowserBackend", "make_snapshot"]
