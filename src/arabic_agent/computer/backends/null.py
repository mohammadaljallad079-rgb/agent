"""A computer backend that is explicitly not available.

Used on platforms where no backend is implemented yet (Windows, macOS) and in
tests that need the *unavailable* path. It reports why it is unavailable so the
computer tools fail honestly instead of pretending to control a machine.
"""

from __future__ import annotations

from ...core.enums import PlatformKind
from ..base import CapturedImage, ComputerBackend, PointerButton, ScreenMetadata, WindowInfo
from ..coords import Point


class NullComputerBackend(ComputerBackend):
    """An unavailable backend; every operation refuses with a clear reason."""

    name = "none"
    platform = PlatformKind.UNKNOWN
    available = False

    def __init__(
        self, reason: str = "no computer backend is implemented for this platform"
    ) -> None:
        self.unavailable_reason = reason

    async def get_screen_metadata(self) -> ScreenMetadata:
        self.require_available()
        raise AssertionError("unreachable")

    async def capture_screen(self, *, monitor_id: int | None = None) -> CapturedImage:
        self.require_available()
        raise AssertionError("unreachable")

    async def list_windows(self) -> list[WindowInfo]:
        self.require_available()
        return []

    async def pointer_move(self, point: Point) -> None:
        self.require_available()

    async def pointer_click(
        self, point: Point, *, button: PointerButton = PointerButton.LEFT, double: bool = False
    ) -> None:
        self.require_available()

    async def pointer_scroll(self, *, amount: int, point: Point | None = None) -> None:
        self.require_available()

    async def keyboard_type(self, text: str) -> None:
        self.require_available()

    async def keyboard_key(self, key: str) -> None:
        self.require_available()

    async def keyboard_hotkey(self, keys: list[str]) -> None:
        self.require_available()


__all__ = ["NullComputerBackend"]
