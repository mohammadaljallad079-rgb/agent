"""Windows computer backend tests (run on any OS).

The backend is a real Win32 implementation, but the Windows API itself is behind
the injectable :class:`Win32Adapter` seam. These tests drive the *whole backend*
— coordinate conversion, monitor selection, input routing, the async offload —
against a scripted adapter, so the logic is proven without a Windows host. The
only part not exercised here is :class:`CtypesWin32Adapter`, which is
``pragma: no cover`` Windows-only glue and is covered by the Windows CI job.

Nothing here claims the product works on Windows: it proves the backend's
behaviour is correct given the values the OS would return.
"""

from __future__ import annotations

import sys

import pytest

from arabic_agent.computer.backends import build_computer_backend
from arabic_agent.computer.backends.null import NullComputerBackend
from arabic_agent.computer.backends.windows import (
    MAX_WINDOWS,
    CtypesWin32Adapter,
    Win32Adapter,
    WindowsBackend,
)
from arabic_agent.computer.base import CapturedImage, PointerButton, WindowInfo
from arabic_agent.computer.capture import encode_png
from arabic_agent.computer.coords import CoordinateSpace, MonitorInfo, Point, Rect
from arabic_agent.core.enums import PlatformKind
from arabic_agent.core.errors import ConfigurationError, ExternalServiceError


class FakeWin32Adapter(Win32Adapter):
    """A scripted adapter that records every call the backend makes."""

    available = True
    unavailable_reason = ""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.monitor_list = [
            MonitorInfo(
                id=0, name="\\\\.\\DISPLAY1", width=1920, height=1080, scale=1.0, primary=True
            ),
            MonitorInfo(id=1, name="\\\\.\\DISPLAY2", x=1920, width=1920, height=1080, scale=1.5),
        ]
        self.cursor = Point(x=100, y=200, space=CoordinateSpace.PHYSICAL)
        self.windows = [WindowInfo(id="7", title="Notepad", active=True)]

    def _record(self, name: str, *args: object) -> None:
        self.calls.append((name, args))

    def monitors(self) -> list[MonitorInfo]:
        self._record("monitors")
        return list(self.monitor_list)

    def cursor_position(self) -> Point | None:
        self._record("cursor_position")
        return self.cursor

    def capture(self, region: Rect | None) -> CapturedImage:
        self._record("capture", region)
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - depends on the install
            pytest.skip(f"Pillow (computer extra) not installed: {exc}")

        width = region.width if region else 3840
        height = region.height if region else 1080
        image = Image.new("RGB", (width, height), (10, 20, 30))
        return CapturedImage(
            data=encode_png(image),
            width=width,
            height=height,
            region=region,
            space=CoordinateSpace.PHYSICAL,
        )

    def list_windows(self) -> list[WindowInfo]:
        self._record("list_windows")
        return list(self.windows)

    def focus_window(self, window_id: str) -> bool:
        self._record("focus_window", window_id)
        return window_id == "7"

    def pointer_move(self, x: int, y: int) -> None:
        self._record("pointer_move", x, y)

    def pointer_click(self, x: int, y: int, button: PointerButton, double: bool) -> None:
        self._record("pointer_click", x, y, button, double)

    def pointer_scroll(self, amount: int) -> None:
        self._record("pointer_scroll", amount)

    def type_text(self, text: str) -> None:
        self._record("type_text", text)

    def press_key(self, key: str) -> None:
        self._record("press_key", key)

    def hotkey(self, keys: list[str]) -> None:
        self._record("hotkey", tuple(keys))


def _backend() -> tuple[WindowsBackend, FakeWin32Adapter]:
    adapter = FakeWin32Adapter()
    return WindowsBackend(adapter=adapter), adapter


# --- Availability -----------------------------------------------------------
def test_fake_adapter_backend_is_available():
    backend, _ = _backend()
    assert backend.available is True
    assert backend.platform is PlatformKind.WINDOWS
    assert backend.name == "windows"
    assert backend.supports_windows is True


def test_unavailable_adapter_reports_reason():
    class Offline(Win32Adapter):
        available = False
        unavailable_reason = "not running on Windows"

        def monitors(self):  # pragma: no cover - never called
            return []

        def cursor_position(self):  # pragma: no cover
            return None

        def capture(self, region):  # pragma: no cover
            raise AssertionError

        def list_windows(self):  # pragma: no cover
            return []

        def focus_window(self, window_id):  # pragma: no cover
            return False

        def pointer_move(self, x, y):  # pragma: no cover
            pass

        def pointer_click(self, x, y, button, double):  # pragma: no cover
            pass

        def pointer_scroll(self, amount):  # pragma: no cover
            pass

        def type_text(self, text):  # pragma: no cover
            pass

        def press_key(self, key):  # pragma: no cover
            pass

        def hotkey(self, keys):  # pragma: no cover
            pass

    backend = WindowsBackend(adapter=Offline())
    assert backend.available is False
    assert backend.unavailable_reason == "not running on Windows"
    with pytest.raises(ConfigurationError):
        backend.require_available()
    # A backend that is not available returns no windows rather than raising.
    import asyncio

    assert asyncio.run(backend.list_windows()) == []


def test_ctypes_adapter_is_honest_off_windows():
    adapter = CtypesWin32Adapter()
    if sys.platform.startswith("win"):  # pragma: no cover - Windows CI only
        assert adapter.available is True
    else:
        assert adapter.available is False
        assert "not running on Windows" in adapter.unavailable_reason


def test_factory_returns_null_backend_off_windows():
    """On this (non-Windows) host the real adapter is unavailable, so the
    factory must return the honest null backend with a reason."""
    backend = build_computer_backend(platform=PlatformKind.WINDOWS)
    if sys.platform.startswith("win"):  # pragma: no cover - Windows CI only
        assert isinstance(backend, WindowsBackend)
    else:
        assert isinstance(backend, NullComputerBackend)
        assert backend.unavailable_reason


# --- Perception -------------------------------------------------------------
async def test_screen_metadata_reports_monitors_and_cursor():
    backend, _ = _backend()
    meta = await backend.get_screen_metadata()
    assert meta.backend == "windows"
    assert meta.platform is PlatformKind.WINDOWS
    assert len(meta.monitors) == 2
    assert meta.primary is not None and meta.primary.width == 1920
    assert meta.cursor is not None and meta.cursor.x == 100
    assert meta.virtual_bounds is not None and meta.virtual_bounds.width == 3840


async def test_capture_whole_screen():
    backend, adapter = _backend()
    image = await backend.capture_screen()
    assert image.width == 3840
    assert image.mime_type == "image/png"
    assert image.region is None
    assert ("capture", (None,)) in adapter.calls


async def test_capture_specific_monitor_uses_its_region():
    backend, adapter = _backend()
    image = await backend.capture_screen(monitor_id=1)
    assert image.monitor_id == 1
    assert image.region is not None
    assert image.region.x == 1920
    assert image.width == 1920
    assert ("capture", (image.region,)) in adapter.calls


async def test_capture_unknown_monitor_fails_clearly():
    backend, _ = _backend()
    with pytest.raises(ExternalServiceError):
        await backend.capture_screen(monitor_id=99)


async def test_list_windows_and_active_window():
    backend, _ = _backend()
    windows = await backend.list_windows()
    assert [w.id for w in windows] == ["7"]
    active = await backend.get_active_window()
    assert active is not None and active.title == "Notepad"


async def test_focus_window_delegates():
    backend, adapter = _backend()
    assert await backend.focus_window("7") is True
    assert await backend.focus_window("404") is False
    assert ("focus_window", ("7",)) in adapter.calls


# --- Input + coordinate conversion -----------------------------------------
async def test_pointer_move_physical_passthrough():
    backend, adapter = _backend()
    await backend.pointer_move(Point(x=50, y=60, space=CoordinateSpace.PHYSICAL))
    assert ("pointer_move", (50, 60)) in adapter.calls


async def test_pointer_move_logical_is_scaled_for_the_monitor():
    """A logical point on a 150% monitor is converted to physical pixels."""
    backend, adapter = _backend()
    # Point (2020, 100) is inside monitor 1 (x>=1920), scale 1.5.
    await backend.pointer_move(Point(x=2020, y=100, space=CoordinateSpace.LOGICAL))
    # to_physical: x = round(1920*1.5) + round((2020-1920)*1.5) = 2880 + 150 = 3030
    #              y = round(0) + round(100*1.5) = 150
    assert ("pointer_move", (3030, 150)) in adapter.calls


async def test_pointer_move_logical_on_unscaled_monitor_is_identity():
    backend, adapter = _backend()
    await backend.pointer_move(Point(x=100, y=100, space=CoordinateSpace.LOGICAL))
    assert ("pointer_move", (100, 100)) in adapter.calls


async def test_pointer_click_delegates_with_button_and_double():
    backend, adapter = _backend()
    await backend.pointer_click(
        Point(x=10, y=20, space=CoordinateSpace.PHYSICAL),
        button=PointerButton.RIGHT,
        double=True,
    )
    assert ("pointer_click", (10, 20, PointerButton.RIGHT, True)) in adapter.calls


async def test_pointer_scroll_without_point_does_not_move():
    backend, adapter = _backend()
    await backend.pointer_scroll(amount=3)
    assert ("pointer_scroll", (3,)) in adapter.calls
    assert not any(name == "pointer_move" for name, _ in adapter.calls)


async def test_pointer_scroll_with_point_moves_then_scrolls():
    backend, adapter = _backend()
    await backend.pointer_scroll(amount=-2, point=Point(x=5, y=6, space=CoordinateSpace.PHYSICAL))
    names = [name for name, _ in adapter.calls]
    assert names == ["monitors", "pointer_move", "pointer_scroll"]


async def test_keyboard_type_passes_unicode_through():
    backend, adapter = _backend()
    text = "مرحبا بالعالم"
    await backend.keyboard_type(text)
    assert ("type_text", (text,)) in adapter.calls


async def test_keyboard_key_and_hotkey_delegate():
    backend, adapter = _backend()
    await backend.keyboard_key("Return")
    await backend.keyboard_hotkey(["ctrl", "s"])
    assert ("press_key", ("Return",)) in adapter.calls
    assert ("hotkey", (("ctrl", "s"),)) in adapter.calls


def test_max_windows_is_bounded():
    assert MAX_WINDOWS > 0
