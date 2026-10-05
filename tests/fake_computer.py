"""Test doubles for the computer subsystem.

These are *doubles for the OS*, not for the code under test: the perception
layer, policy, grounding, runtime, tools and audit all run for real against a
scripted backend. Only the X server / window manager / OCR binary is replaced,
so unit tests are deterministic and need no display, while the real X11 path is
covered separately by the E2E suite.
"""

from __future__ import annotations

from arabic_agent.computer.base import (
    CapturedImage,
    ComputerBackend,
    PointerButton,
    ScreenMetadata,
    WindowInfo,
)
from arabic_agent.computer.capture import encode_png
from arabic_agent.computer.coords import CoordinateSpace, MonitorInfo, Point, Rect
from arabic_agent.computer.ocr import OcrResult, OcrWord
from arabic_agent.core.enums import PlatformKind


def make_image(width: int = 64, height: int = 48, color: tuple[int, int, int] = (0, 0, 0)):
    """A tiny in-memory PIL image."""
    from PIL import Image

    return Image.new("RGB", (width, height), color)


def make_pattern_image(width: int = 64, height: int = 48) -> object:
    """A tiny image with a strong left/right gradient.

    A uniform image hashes identically under a difference hash (adjacent pixels
    are equal), so change-detection tests need real structure to compare.
    """
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, width // 2, height], fill=(255, 255, 255))
    return image


class FakeOcr:
    """An OCR engine returning scripted words; runs synchronously."""

    name = "fake-ocr"
    available = True

    def __init__(self, words: list[OcrWord] | None = None) -> None:
        self._words = words or []
        self.calls = 0

    async def recognize(self, image, *, region: Rect | None = None) -> OcrResult:
        self.calls += 1
        return OcrResult(words=tuple(self._words), languages=("ara", "eng"), available=True)


class FakeComputerBackend(ComputerBackend):
    """A scripted backend that records calls and can simulate screen change."""

    name = "fake"
    platform = PlatformKind.LINUX
    available = True
    supports_windows = True

    def __init__(
        self,
        *,
        width: int = 1280,
        height: int = 900,
        image=None,
        windows: list[WindowInfo] | None = None,
        monitor_id: int = 0,
    ) -> None:
        self._monitor = MonitorInfo(id=monitor_id, name="screen", width=width, height=height)
        self._image = image if image is not None else make_image(width, height)
        self._windows = windows if windows is not None else []
        self._active = next((w for w in self._windows if w.active), None)
        self.calls: list[tuple[str, object]] = []
        self._capture_count = 0
        self.closed = False

    def mutate(self, image) -> None:
        """Change what the next capture returns (simulates a real screen change)."""
        self._image = image

    async def get_screen_metadata(self) -> ScreenMetadata:
        from arabic_agent.computer.coords import virtual_bounds

        return ScreenMetadata(
            monitors=(self._monitor,),
            virtual_bounds=virtual_bounds([self._monitor]),
            cursor=Point(x=0, y=0, space=CoordinateSpace.PHYSICAL),
            platform=PlatformKind.LINUX,
            backend=self.name,
        )

    async def capture_screen(self, *, monitor_id: int | None = None) -> CapturedImage:
        self._capture_count += 1
        return CapturedImage(
            data=encode_png(self._image),
            width=self._image.width,
            height=self._image.height,
            monitor_id=monitor_id,
            space=CoordinateSpace.PHYSICAL,
        )

    async def list_windows(self) -> list[WindowInfo]:
        return list(self._windows)

    async def get_active_window(self) -> WindowInfo | None:
        return self._active

    async def focus_window(self, window_id: str) -> bool:
        self.calls.append(("focus_window", window_id))
        return window_id in {w.id for w in self._windows}

    async def pointer_move(self, point: Point) -> None:
        self.calls.append(("pointer_move", point.as_tuple()))

    async def pointer_click(
        self, point: Point, *, button: PointerButton = PointerButton.LEFT, double: bool = False
    ) -> None:
        self.calls.append(("pointer_click", (point.as_tuple(), button.value, double)))

    async def pointer_scroll(self, *, amount: int, point: Point | None = None) -> None:
        self.calls.append(("pointer_scroll", (amount, None if point is None else point.as_tuple())))

    async def keyboard_type(self, text: str) -> None:
        self.calls.append(("keyboard_type", text))

    async def keyboard_key(self, key: str) -> None:
        self.calls.append(("keyboard_key", key))

    async def keyboard_hotkey(self, keys: list[str]) -> None:
        self.calls.append(("keyboard_hotkey", list(keys)))

    async def aclose(self) -> None:
        self.closed = True


__all__ = ["FakeComputerBackend", "FakeOcr", "make_image"]
