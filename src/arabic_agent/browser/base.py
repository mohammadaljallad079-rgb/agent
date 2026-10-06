"""Browser backend abstraction.

The agent must never see a raw automation API. A :class:`BrowserBackend` is the
single seam between the browser tools and whatever actually drives a browser --
Playwright/Chromium here, something else later. The interface is intentionally
small, page-scoped and platform-neutral.

Everything a backend returns is a plain, serialisable model so it can cross the
tool/perception boundary and be budgeted, redacted and audited. A backend
declares whether it is *available* on the current machine and, if not, *why* --
an unavailable backend raises a clear error instead of pretending to work.

The backend performs real browser operations only. Policy, permissions,
approvals, rate limits, the data boundary and the audit trail live *above* this
interface and are never the backend's responsibility.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class BrowserPageState(StrEnum):
    """Coarse lifecycle state of a page, for freshness reasoning."""

    EMPTY = "empty"
    LOADING = "loading"
    READY = "ready"
    ERROR = "error"
    CLOSED = "closed"


class TabInfo(BaseModel):
    """A browser tab (page) known to the backend."""

    model_config = ConfigDict(frozen=True)

    id: str
    url: str = ""
    title: str = ""
    active: bool = False
    state: BrowserPageState = BrowserPageState.EMPTY


class ElementInfo(BaseModel):
    """A normalised, model-facing description of one interactive element.

    Elements carry a stable ``ref`` the model can act on without knowing a CSS
    selector, plus the semantic attributes an accessibility-first interaction
    needs (role, accessible name, label, text).
    """

    model_config = ConfigDict(frozen=True)

    ref: str
    role: str = ""
    name: str = ""
    tag: str = ""
    text: str = ""
    value: str = ""
    href: str = ""
    placeholder: str = ""
    input_type: str = ""
    checked: bool | None = None
    disabled: bool = False
    #: Bounding box in CSS pixels (x, y, width, height), when known.
    box: tuple[float, float, float, float] | None = None


class FormFieldInfo(BaseModel):
    """One field of a form, with its semantic label and current value."""

    model_config = ConfigDict(frozen=True)

    ref: str
    name: str = ""
    label: str = ""
    input_type: str = "text"
    value: str = ""
    placeholder: str = ""
    required: bool = False
    options: tuple[str, ...] = ()
    checked: bool | None = None


class FormInfo(BaseModel):
    """A form: its semantic identity and its fields."""

    model_config = ConfigDict(frozen=True)

    ref: str
    name: str = ""
    action: str = ""
    method: str = "get"
    fields: tuple[FormFieldInfo, ...] = ()


class DialogInfo(BaseModel):
    """A JavaScript dialog (alert/confirm/prompt) currently open, if any."""

    model_config = ConfigDict(frozen=True)

    type: str
    message: str = ""


class DownloadedFile(BaseModel):
    """A completed download, confined to the browser's download directory."""

    model_config = ConfigDict(frozen=True)

    suggested_filename: str
    path: str
    size_bytes: int = 0
    url: str = ""


class Screenshot(BaseModel):
    """A page screenshot as encoded bytes plus geometry (in-memory only)."""

    model_config = ConfigDict(frozen=True)

    data: bytes
    mime_type: str = "image/png"
    width: int = 0
    height: int = 0

    @property
    def size_bytes(self) -> int:
        return len(self.data)


class BrowserBackend(ABC):
    """Contract every browser automation backend implements."""

    #: Stable identifier used in configuration and audit (e.g. ``"playwright"``).
    name: str
    #: Whether this backend can actually run on the current machine.
    available: bool = False
    #: Why it is unavailable, when :attr:`available` is ``False``.
    unavailable_reason: str = "not implemented"
    #: Whether the backend runs a visible (headed) browser.
    headed: bool = False

    def require_available(self) -> None:
        if not self.available:
            from ..core.errors import ConfigurationError

            raise ConfigurationError(
                f"browser backend {self.name!r} is unavailable: {self.unavailable_reason}"
            )

    # -- Lifecycle -------------------------------------------------------
    @abstractmethod
    async def start(self) -> None:
        """Launch the browser and open an initial page."""

    @abstractmethod
    async def aclose(self) -> None:
        """Close the browser and release every resource it owns."""

    # -- Tabs ------------------------------------------------------------
    @abstractmethod
    async def list_tabs(self) -> list[TabInfo]:
        """List open tabs, marking the active one."""

    @abstractmethod
    async def open_tab(self, url: str = "") -> TabInfo:
        """Open a new tab, optionally navigating it."""

    @abstractmethod
    async def switch_tab(self, tab_id: str) -> TabInfo:
        """Make ``tab_id`` the active tab."""

    @abstractmethod
    async def close_tab(self, tab_id: str) -> None:
        """Close a tab."""

    @abstractmethod
    async def active_tab(self) -> TabInfo:
        """Return the active tab."""

    # -- Navigation ------------------------------------------------------
    @abstractmethod
    async def navigate(self, url: str, *, timeout_ms: int | None = None) -> TabInfo:
        """Navigate the active tab to ``url`` and wait for readiness."""

    @abstractmethod
    async def go_back(self) -> TabInfo: ...

    @abstractmethod
    async def go_forward(self) -> TabInfo: ...

    @abstractmethod
    async def reload(self) -> TabInfo: ...

    # -- Observation -----------------------------------------------------
    @abstractmethod
    async def snapshot(self, *, max_elements: int = 60) -> PageSnapshot:
        """Return a bounded, normalised observation of the active page."""

    @abstractmethod
    async def screenshot(self, *, full_page: bool = False) -> Screenshot:
        """Capture the active page as PNG bytes (in memory only)."""

    # -- Interaction -----------------------------------------------------
    @abstractmethod
    async def click(self, ref: str, *, timeout_ms: int | None = None) -> None:
        """Click the element identified by a semantic ``ref``."""

    @abstractmethod
    async def type_text(self, ref: str, text: str, *, clear: bool = True) -> None:
        """Type ``text`` into the field identified by ``ref``."""

    @abstractmethod
    async def press_key(self, key: str, *, ref: str | None = None) -> None:
        """Press a named key, optionally focused on an element."""

    @abstractmethod
    async def select_option(self, ref: str, value: str) -> None:
        """Select an option in a ``<select>`` by value or label."""

    @abstractmethod
    async def set_checked(self, ref: str, *, checked: bool) -> None:
        """Set a checkbox/radio to a checked state."""

    @abstractmethod
    async def scroll(self, *, amount: int, ref: str | None = None) -> None:
        """Scroll the page (positive = down)."""

    @abstractmethod
    async def wait_for(self, *, kind: str, value: str = "", timeout_ms: int | None = None) -> None:
        """Wait for a page condition (``load``, ``text``, ``url``, ``element``)."""

    # -- Files -----------------------------------------------------------
    @abstractmethod
    async def download(self, ref: str, *, timeout_ms: int | None = None) -> DownloadedFile:
        """Click a download-triggering element and save the file."""

    @abstractmethod
    async def upload(self, ref: str, path: str) -> None:
        """Attach a local file (already sandbox-approved) to a file input."""

    # -- Dialogs ---------------------------------------------------------
    @abstractmethod
    async def dialog(self) -> DialogInfo | None:
        """Return the currently open dialog, if any."""

    @abstractmethod
    async def accept_dialog(self, *, prompt_text: str | None = None) -> None:
        """Accept the open dialog (optionally answering a prompt)."""

    @abstractmethod
    async def dismiss_dialog(self) -> None:
        """Dismiss the open dialog."""


class PageSnapshot(BaseModel):
    """A bounded, normalised observation of one page.

    This is the raw material the runtime turns into a :class:`BrowserObservation`.
    It never contains a raw DOM dump: only a bounded set of interactive elements,
    headings, forms and a bounded text extract.
    """

    model_config = ConfigDict(frozen=True)

    url: str = ""
    title: str = ""
    state: BrowserPageState = BrowserPageState.EMPTY
    text: str = ""
    text_truncated: bool = False
    headings: tuple[str, ...] = ()
    elements: tuple[ElementInfo, ...] = ()
    forms: tuple[FormInfo, ...] = ()
    links: tuple[ElementInfo, ...] = ()
    dialog: DialogInfo | None = None
    iframe_count: int = 0
    #: Scroll metrics, so the agent knows whether more content may exist.
    scroll_y: float = 0.0
    scroll_height: float = 0.0
    viewport_height: float = 0.0
    notes: tuple[str, ...] = Field(default_factory=tuple)

    @property
    def at_bottom(self) -> bool:
        if self.scroll_height <= 0:
            return True
        return self.scroll_y + self.viewport_height >= self.scroll_height - 4

    @property
    def has_more_below(self) -> bool:
        return not self.at_bottom


__all__ = [
    "BrowserBackend",
    "BrowserPageState",
    "DialogInfo",
    "DownloadedFile",
    "ElementInfo",
    "FormFieldInfo",
    "FormInfo",
    "PageSnapshot",
    "Screenshot",
    "TabInfo",
]
