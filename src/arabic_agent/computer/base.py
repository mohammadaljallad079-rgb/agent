"""Computer environment abstraction.

The agent must never see a raw OS API. A :class:`ComputerBackend` is the single
seam between the agent's computer tools and whatever actually controls the
machine — an X11 session here, a Windows or macOS session later. The interface
is intentionally small and platform-neutral:

    capture_screen()   get_screen_metadata()   get_cursor_position()
    list_monitors()    list_windows()          get_active_window()
    focus_window()     pointer_move()          pointer_click()
    pointer_scroll()   keyboard_type()         keyboard_key()  keyboard_hotkey()

A backend declares whether it is *available* on the current platform and, if
not, *why* — an unavailable backend raises a clear error instead of pretending
to work. The concrete adapters live in :mod:`arabic_agent.computer.backends`.

Everything a backend returns is a plain, serialisable model so it can cross the
tool/perception boundary and be budgeted, redacted and audited.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from ..core.enums import PlatformKind
from .coords import CoordinateSpace, MonitorInfo, Point, Rect


class PointerButton(StrEnum):
    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"


class CapturedImage(BaseModel):
    """A screen capture as encoded bytes plus its geometry.

    The image is held in memory only; the caller decides whether it is stored,
    resized, sent to a model or discarded. It is never written to the audit log.
    """

    model_config = ConfigDict(frozen=True)

    data: bytes
    mime_type: str = "image/png"
    width: int
    height: int
    #: Which monitor this capture covers, or ``None`` for the whole virtual screen.
    monitor_id: int | None = None
    #: The screen region this capture covers, in the capture's coordinate space.
    region: Rect | None = None
    space: CoordinateSpace = CoordinateSpace.PHYSICAL
    captured_at: float = 0.0

    @property
    def size_bytes(self) -> int:
        return len(self.data)


class ScreenMetadata(BaseModel):
    """Describes the current display configuration without capturing pixels."""

    model_config = ConfigDict(frozen=True)

    monitors: tuple[MonitorInfo, ...] = ()
    #: The virtual-screen bounding rectangle across all monitors.
    virtual_bounds: Rect | None = None
    cursor: Point | None = None
    platform: PlatformKind = PlatformKind.UNKNOWN
    backend: str = ""
    #: Human-readable note when some metadata could not be determined.
    notes: tuple[str, ...] = ()

    @property
    def primary(self) -> MonitorInfo | None:
        for monitor in self.monitors:
            if monitor.primary:
                return monitor
        return self.monitors[0] if self.monitors else None


class WindowInfo(BaseModel):
    """A top-level window known to the window manager."""

    model_config = ConfigDict(frozen=True)

    id: str
    title: str = ""
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    active: bool = False
    #: Whether the window manager reports this window as visible/mapped.
    visible: bool = True

    @property
    def rect(self) -> Rect:
        return Rect(x=self.x, y=self.y, width=max(self.width, 0), height=max(self.height, 0))


class ComputerBackend(ABC):
    """Contract every platform backend implements.

    Implementations perform real OS operations. The policy, permission,
    approval, rate-limit and audit layers live *above* this interface and are
    never the backend's responsibility.
    """

    #: Stable identifier used in configuration and audit (e.g. ``"x11"``).
    name: str
    #: The OS family this backend targets.
    platform: PlatformKind = PlatformKind.UNKNOWN
    #: Whether this backend can actually run on the current machine.
    available: bool = False
    #: Why it is unavailable, when :attr:`available` is ``False``.
    unavailable_reason: str = "not implemented"
    #: Which capabilities the backend supports, so a tool can fail honestly.
    supports_windows: bool = False

    def require_available(self) -> None:
        if not self.available:
            from ..core.errors import ConfigurationError

            raise ConfigurationError(
                f"computer backend {self.name!r} is unavailable: {self.unavailable_reason}"
            )

    # -- Perception ------------------------------------------------------
    @abstractmethod
    async def get_screen_metadata(self) -> ScreenMetadata:
        """Return display configuration, cursor and platform info."""

    @abstractmethod
    async def capture_screen(self, *, monitor_id: int | None = None) -> CapturedImage:
        """Capture one monitor (or the whole virtual screen when ``None``)."""

    async def get_cursor_position(self) -> Point | None:
        metadata = await self.get_screen_metadata()
        return metadata.cursor

    # -- Windows ---------------------------------------------------------
    async def list_windows(self) -> list[WindowInfo]:
        """List top-level windows. Backends without WM support return ``[]``."""
        return []

    async def get_active_window(self) -> WindowInfo | None:
        for window in await self.list_windows():
            if window.active:
                return window
        return None

    async def focus_window(self, window_id: str) -> bool:
        """Focus a window by id; return whether it succeeded."""
        raise NotImplementedError(f"{self.name} backend does not support window focus")

    # -- Input -----------------------------------------------------------
    @abstractmethod
    async def pointer_move(self, point: Point) -> None:
        """Move the pointer to an absolute position."""

    @abstractmethod
    async def pointer_click(
        self, point: Point, *, button: PointerButton = PointerButton.LEFT, double: bool = False
    ) -> None:
        """Move to ``point`` and click (or double-click) it."""

    @abstractmethod
    async def pointer_scroll(self, *, amount: int, point: Point | None = None) -> None:
        """Scroll vertically by ``amount`` (positive = up, negative = down)."""

    @abstractmethod
    async def keyboard_type(self, text: str) -> None:
        """Type Unicode text, including Arabic, at the current focus."""

    @abstractmethod
    async def keyboard_key(self, key: str) -> None:
        """Press and release a single named key (e.g. ``Return``, ``Tab``)."""

    @abstractmethod
    async def keyboard_hotkey(self, keys: list[str]) -> None:
        """Press a chord of keys together (e.g. ``["ctrl", "s"]``)."""

    async def aclose(self) -> None:
        return None


__all__ = [
    "CapturedImage",
    "ComputerBackend",
    "PointerButton",
    "ScreenMetadata",
    "WindowInfo",
]
