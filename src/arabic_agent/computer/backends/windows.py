"""Windows computer backend (Win32).

The Windows half of the computer-use seam. It implements the same
:class:`~arabic_agent.computer.base.ComputerBackend` contract as the X11
backend, so the perception layer, grounding, policy, budget and audit above it
are unchanged and platform-independent.

Two layers, deliberately separated:

* :class:`Win32Adapter` is the *only* place that touches the Windows API. It is
  a narrow, injectable seam: every method returns plain values, and a test can
  substitute a fake adapter to exercise the whole backend without Windows.
* :class:`WindowsBackend` translates those raw values into the shared
  ``CapturedImage``/``ScreenMetadata``/``WindowInfo`` models, converts logical
  (DPI-scaled) points to the physical pixels ``SendInput`` expects, and reports
  availability honestly.

The real adapter is :class:`CtypesWin32Adapter`, built on ``ctypes`` and the
Windows API:

* monitors and scale from ``EnumDisplayMonitors`` + ``GetDpiForMonitor``;
* capture through Pillow's native Windows screen grab (``BitBlt`` underneath),
  optionally restricted to one monitor;
* input through ``SendInput`` — absolute pointer moves/clicks/wheel, and Unicode
  keyboard injection so Arabic text is typed correctly regardless of the active
  layout;
* window enumeration/focus from ``EnumWindows``/``GetForegroundWindow``/
  ``SetForegroundWindow``.

Because ``ctypes.windll`` only exists on Windows, the module imports and the
class constructs on any OS: off Windows the adapter reports itself unavailable
with a clear reason, and the backend then reports unavailable too — never a
silent "available" that fails on first use.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, TypeVar

from ...core.enums import PlatformKind
from ...core.errors import ExternalServiceError
from ..base import CapturedImage, ComputerBackend, PointerButton, ScreenMetadata, WindowInfo
from ..coords import CoordinateSpace, MonitorInfo, Point, Rect, virtual_bounds

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

#: Upper bound on windows returned by one enumeration (bounded observation).
MAX_WINDOWS = 200

#: Named keys the keyboard tools may press, mapped to Windows virtual-key codes.
#: Matches the allow-list vocabulary of :mod:`arabic_agent.computer.policy`.
_KEY_CODES: dict[str, int] = {
    "return": 0x0D,
    "enter": 0x0D,
    "tab": 0x09,
    "escape": 0x1B,
    "esc": 0x1B,
    "backspace": 0x08,
    "delete": 0x2E,
    "space": 0x20,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "meta": 0x5B,
    "super": 0x5B,
    "f5": 0x74,
    "f11": 0x7A,
}
#: Single letters A-Z map directly onto their virtual-key code.
for _letter in "abcdefghijklmnopqrstuvwxyz":
    _KEY_CODES.setdefault(_letter, ord(_letter.upper()))

#: Mouse buttons, as the ``SendInput`` mouse-data flags.
_MOUSE_FLAGS: dict[PointerButton, tuple[int, int]] = {
    PointerButton.LEFT: (0x0002, 0x0004),  # LEFTDOWN, LEFTUP
    PointerButton.RIGHT: (0x0008, 0x0010),  # RIGHTDOWN, RIGHTUP
    PointerButton.MIDDLE: (0x0020, 0x0040),  # MIDDLEDOWN, MIDDLEUP
}


class Win32Adapter(ABC):
    """The narrow Windows-API seam the backend depends on.

    A concrete implementation performs real Win32 calls; a fake can be injected
    in tests so the backend logic is exercised without a Windows host.
    """

    #: Whether the adapter can actually drive this machine.
    available: bool = False
    #: Why it cannot, when :attr:`available` is ``False``.
    unavailable_reason: str = "not implemented"

    def require_available(self) -> None:
        if not self.available:
            raise ExternalServiceError(f"Win32 adapter unavailable: {self.unavailable_reason}")

    @abstractmethod
    def monitors(self) -> list[MonitorInfo]:
        """The display layout, in logical pixels, with per-monitor DPI scale."""

    @abstractmethod
    def cursor_position(self) -> Point | None:
        """The pointer position in physical pixels, or ``None`` if unknown."""

    @abstractmethod
    def capture(self, region: Rect | None) -> CapturedImage:
        """Capture the whole virtual screen, or ``region`` if given."""

    @abstractmethod
    def list_windows(self) -> list[WindowInfo]:
        """Enumerate top-level, visible windows (bounded)."""

    @abstractmethod
    def focus_window(self, window_id: str) -> bool:
        """Bring a window to the foreground; return whether it succeeded."""

    @abstractmethod
    def pointer_move(self, x: int, y: int) -> None:
        """Move the pointer to an absolute physical position."""

    @abstractmethod
    def pointer_click(self, x: int, y: int, button: PointerButton, double: bool) -> None:
        """Move and click (or double-click) at an absolute physical position."""

    @abstractmethod
    def pointer_scroll(self, amount: int) -> None:
        """Scroll the wheel (positive = up)."""

    @abstractmethod
    def type_text(self, text: str) -> None:
        """Type Unicode text at the current focus."""

    @abstractmethod
    def press_key(self, key: str) -> None:
        """Press and release one named key."""

    @abstractmethod
    def hotkey(self, keys: list[str]) -> None:
        """Press a chord of named keys together."""


class CtypesWin32Adapter(Win32Adapter):
    """The real adapter, implemented over ``ctypes`` and the Win32 API.

    Construction is side-effect free: DPI awareness is set once here (so a
    capture returns physical pixels and a click lands where it was aimed), and
    every capability is probed. If anything required is missing the adapter
    reports itself unavailable with the specific reason.
    """

    #: ``SendInput`` key/mouse event types and flags.
    _INPUT_MOUSE = 0
    _INPUT_KEYBOARD = 1
    _KEYEVENTF_KEYUP = 0x0002
    _KEYEVENTF_UNICODE = 0x0004
    _MOUSEEVENTF_MOVE = 0x0001
    _MOUSEEVENTF_ABSOLUTE = 0x8000
    _MOUSEEVENTF_WHEEL = 0x0800
    _MOUSEEVENTF_VIRTUALDESK = 0x4000

    def __init__(self) -> None:
        self._user32 = None
        self._shcore = None
        self._windll = getattr(ctypes, "windll", None)
        if not sys.platform.startswith("win") or self._windll is None:
            self.unavailable_reason = "not running on Windows"
            return
        try:
            self._user32 = self._windll.user32
            self._shcore = getattr(self._windll, "shcore", None)
        except (AttributeError, OSError) as exc:  # pragma: no cover - Windows only
            self.unavailable_reason = f"could not load the Win32 API: {exc}"
            return
        self._set_dpi_awareness()
        self.available = True
        self.unavailable_reason = ""

    # -- Setup -----------------------------------------------------------
    def _set_dpi_awareness(self) -> None:  # pragma: no cover - Windows only
        """Make the process per-monitor DPI aware, so coordinates are physical."""
        assert self._user32 is not None
        try:
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == -4
            self._user32.SetProcessDpiAwarenessContext(-4)
            return
        except (AttributeError, OSError):
            pass
        try:
            self._user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            logger.debug("could not set DPI awareness; coordinates may be scaled")

    def _dpi_scale(self, handle: int) -> float:  # pragma: no cover - Windows only
        if self._shcore is None:
            return 1.0
        try:
            dpi_x = ctypes.c_uint()
            dpi_y = ctypes.c_uint()
            # MDT_EFFECTIVE_DPI == 0
            if self._shcore.GetDpiForMonitor(handle, 0, dpi_x, dpi_y) == 0:
                return max(dpi_x.value, 1) / 96.0
        except (AttributeError, OSError):
            pass
        return 1.0

    # -- Perception ------------------------------------------------------
    def monitors(self) -> list[MonitorInfo]:  # pragma: no cover - Windows only
        self.require_available()
        import ctypes
        from ctypes import wintypes

        assert self._user32 is not None

        class MONITORINFOEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32),
            ]

        results: list[MonitorInfo] = []

        MonitorEnumProc = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HMONITOR,
            wintypes.HDC,
            ctypes.POINTER(wintypes.RECT),
            wintypes.LPARAM,
        )

        def _callback(handle: Any, _hdc: Any, _rect: Any, _data: Any) -> bool:
            info = MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(MONITORINFOEXW)
            if self._user32.GetMonitorInfoW(handle, ctypes.byref(info)):
                scale = self._dpi_scale(handle)
                results.append(
                    MonitorInfo(
                        id=len(results),
                        name=info.szDevice,
                        x=int(info.rcMonitor.left),
                        y=int(info.rcMonitor.top),
                        width=int(info.rcMonitor.right - info.rcMonitor.left),
                        height=int(info.rcMonitor.bottom - info.rcMonitor.top),
                        scale=scale,
                        primary=bool(info.dwFlags & 1),  # MONITORINFOF_PRIMARY
                    )
                )
            return True

        self._user32.EnumDisplayMonitors(None, None, MonitorEnumProc(_callback), 0)
        return results

    def cursor_position(self) -> Point | None:  # pragma: no cover - Windows only
        self.require_available()
        import ctypes
        from ctypes import wintypes

        assert self._user32 is not None
        point = wintypes.POINT()
        if self._user32.GetCursorPos(ctypes.byref(point)):
            return Point(x=point.x, y=point.y, space=CoordinateSpace.PHYSICAL)
        return None

    def capture(self, region: Rect | None) -> CapturedImage:  # pragma: no cover - Windows only
        self.require_available()
        from PIL import ImageGrab

        bbox = None
        if region is not None:
            bbox = (region.x, region.y, region.right, region.bottom)
        try:
            # ``all_screens`` grabs the whole virtual desktop, including monitors
            # at negative coordinates, which is what a multi-monitor capture needs.
            image = ImageGrab.grab(bbox=bbox, all_screens=True)
        except Exception as exc:  # capture can fail if the session is locked
            raise ExternalServiceError(f"screen capture failed: {exc}") from exc
        from ..capture import encode_png

        return CapturedImage(
            data=encode_png(image),
            width=image.width,
            height=image.height,
            region=region,
            space=CoordinateSpace.PHYSICAL,
            captured_at=time.monotonic(),
        )

    # -- Windows ---------------------------------------------------------
    def list_windows(self) -> list[WindowInfo]:  # pragma: no cover - Windows only
        self.require_available()
        import ctypes
        from ctypes import wintypes

        assert self._user32 is not None
        foreground = self._user32.GetForegroundWindow()
        results: list[WindowInfo] = []

        EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _callback(hwnd: Any, _data: Any) -> bool:
            if len(results) >= MAX_WINDOWS:
                return False
            if not self._user32.IsWindowVisible(hwnd):
                return True
            length = self._user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            self._user32.GetWindowTextW(hwnd, buffer, length + 1)
            rect = wintypes.RECT()
            self._user32.GetWindowRect(hwnd, ctypes.byref(rect))
            results.append(
                WindowInfo(
                    id=str(int(hwnd)),
                    title=buffer.value,
                    x=int(rect.left),
                    y=int(rect.top),
                    width=int(rect.right - rect.left),
                    height=int(rect.bottom - rect.top),
                    active=int(hwnd) == int(foreground),
                    visible=True,
                )
            )
            return True

        self._user32.EnumWindows(EnumProc(_callback), 0)
        return results

    def focus_window(self, window_id: str) -> bool:  # pragma: no cover - Windows only
        self.require_available()
        assert self._user32 is not None
        try:
            hwnd = int(window_id)
        except ValueError:
            return False
        if not self._user32.IsWindow(hwnd):
            return False
        # A background process cannot always steal focus directly; the standard
        # trick is to restore then raise the window before focusing it.
        self._user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        self._user32.SetForegroundWindow(hwnd)
        return int(self._user32.GetForegroundWindow()) == hwnd

    # -- Input -----------------------------------------------------------
    def _send_input(self, inputs: list[object], count: int) -> None:  # pragma: no cover
        assert self._user32 is not None
        sent = self._user32.SendInput(count, inputs, self._input_size())
        if sent != count:
            raise ExternalServiceError("SendInput was rejected by the OS")

    @staticmethod
    def _input_size() -> int:  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p),
            ]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p),
            ]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("union", ctypes.c_byte * 32)]

        return ctypes.sizeof(INPUT)

    def _mouse_event(self, flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> None:
        import ctypes
        from ctypes import wintypes

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p),
            ]

        class _INPUTUNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("pad", ctypes.c_byte * 32)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

        inp = INPUT(type=self._INPUT_MOUSE)
        inp.u.mi = MOUSEINPUT(dx, dy, data, flags, 0, None)
        self._send_input([inp], 1)

    def _absolute(self, x: int, y: int) -> tuple[int, int]:
        """Normalise a physical point to SendInput's 0..65535 absolute space."""
        assert self._user32 is not None
        vx = self._user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
        vy = self._user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
        vw = max(self._user32.GetSystemMetrics(78), 1)  # SM_CXVIRTUALSCREEN
        vh = max(self._user32.GetSystemMetrics(79), 1)  # SM_CYVIRTUALSCREEN
        nx = round((x - vx) * 65535 / vw)
        ny = round((y - vy) * 65535 / vh)
        return nx, ny

    def pointer_move(self, x: int, y: int) -> None:  # pragma: no cover - Windows only
        self.require_available()
        nx, ny = self._absolute(x, y)
        flags = self._MOUSEEVENTF_MOVE | self._MOUSEEVENTF_ABSOLUTE | self._MOUSEEVENTF_VIRTUALDESK
        self._mouse_event(flags, nx, ny)

    def pointer_click(
        self, x: int, y: int, button: PointerButton, double: bool
    ) -> None:  # pragma: no cover - Windows only
        self.require_available()
        self.pointer_move(x, y)
        down, up = _MOUSE_FLAGS[button]
        clicks = 2 if double else 1
        for _ in range(clicks):
            self._mouse_event(down, *self._absolute(x, y))
            self._mouse_event(up, *self._absolute(x, y))

    def pointer_scroll(self, amount: int) -> None:  # pragma: no cover - Windows only
        self.require_available()
        # WHEEL_DELTA is 120 per notch; SendInput expects positive = forward/up.
        self._mouse_event(self._MOUSEEVENTF_WHEEL, data=int(amount) * 120)

    def type_text(self, text: str) -> None:  # pragma: no cover - Windows only
        self.require_available()
        import ctypes
        from ctypes import wintypes

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p),
            ]

        class _INPUTUNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

        for char in text:
            code = ord(char)
            down = INPUT(type=self._INPUT_KEYBOARD)
            down.u.ki = KEYBDINPUT(0, code, self._KEYEVENTF_UNICODE, 0, None)
            up = INPUT(type=self._INPUT_KEYBOARD)
            up.u.ki = KEYBDINPUT(0, code, self._KEYEVENTF_UNICODE | self._KEYEVENTF_KEYUP, 0, None)
            self._send_input([down, up], 2)

    def _key_event(self, vk: int, *, keyup: bool) -> None:
        import ctypes
        from ctypes import wintypes

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p),
            ]

        class _INPUTUNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

        flags = self._KEYEVENTF_KEYUP if keyup else 0
        inp = INPUT(type=self._INPUT_KEYBOARD)
        inp.u.ki = KEYBDINPUT(vk, 0, flags, 0, None)
        self._send_input([inp], 1)

    def press_key(self, key: str) -> None:  # pragma: no cover - Windows only
        self.require_available()
        code = _KEY_CODES.get(key.strip().lower())
        if code is None:
            raise ExternalServiceError(f"unsupported key: {key}")
        self._key_event(code, keyup=False)
        self._key_event(code, keyup=True)

    def hotkey(self, keys: list[str]) -> None:  # pragma: no cover - Windows only
        self.require_available()
        codes: list[int] = []
        for key in keys:
            code = _KEY_CODES.get(key.strip().lower())
            if code is None:
                raise ExternalServiceError(f"unsupported key in chord: {key}")
            codes.append(code)
        for code in codes:
            self._key_event(code, keyup=False)
        for code in reversed(codes):
            self._key_event(code, keyup=True)


class WindowsBackend(ComputerBackend):
    """The Windows computer backend, over an injectable :class:`Win32Adapter`."""

    name = "windows"
    platform = PlatformKind.WINDOWS
    supports_windows = True

    def __init__(self, *, adapter: Win32Adapter | None = None) -> None:
        self._adapter = adapter if adapter is not None else CtypesWin32Adapter()
        self.available = self._adapter.available
        self.unavailable_reason = self._adapter.unavailable_reason

    @property
    def adapter(self) -> Win32Adapter:
        return self._adapter

    # -- helpers ---------------------------------------------------------
    def _monitors(self) -> list[MonitorInfo]:
        return self._adapter.monitors()

    def _physical(self, point: Point, monitors: list[MonitorInfo]) -> Point:
        """Convert a logical point to the physical pixels input expects.

        A logical point is resolved against the monitor that contains it; if it
        is not on any monitor the nearest monitor is used, so a slightly
        out-of-range point is still mapped sensibly rather than rejected here
        (the policy layer is the place that clamps or refuses).
        """
        if point.space is CoordinateSpace.PHYSICAL:
            return point
        target: MonitorInfo | None = None
        for monitor in monitors:
            if monitor.contains(point):
                target = monitor
                break
        if target is None:
            target = min(
                monitors,
                key=lambda m: (point.x - m.x) ** 2 + (point.y - m.y) ** 2,
                default=None,
            )
        if target is None or target.scale == 1.0:
            return point.model_copy(update={"space": CoordinateSpace.PHYSICAL})
        return target.to_physical(point).model_copy(update={"space": CoordinateSpace.PHYSICAL})

    async def _call(self, func: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        """Run a blocking Win32 call off the event loop."""
        return await asyncio.to_thread(func, *args, **kwargs)

    # -- Perception ------------------------------------------------------
    async def get_screen_metadata(self) -> ScreenMetadata:
        self.require_available()
        monitors = await self._call(self._monitors)
        cursor = await self._call(self._adapter.cursor_position)
        return ScreenMetadata(
            monitors=tuple(monitors),
            virtual_bounds=virtual_bounds(monitors) if monitors else None,
            cursor=cursor,
            platform=self.platform,
            backend=self.name,
            notes=() if monitors else ("no monitors reported by the Windows API",),
        )

    async def capture_screen(self, *, monitor_id: int | None = None) -> CapturedImage:
        self.require_available()
        region: Rect | None = None
        if monitor_id is not None:
            monitors = await self._call(self._monitors)
            monitor = next((m for m in monitors if m.id == monitor_id), None)
            if monitor is None:
                raise ExternalServiceError(f"no monitor with id {monitor_id}")
            region = Rect(
                x=monitor.x,
                y=monitor.y,
                width=monitor.width,
                height=monitor.height,
                space=CoordinateSpace.PHYSICAL,
            )
        image = await self._call(self._adapter.capture, region)
        if monitor_id is not None and image.monitor_id is None:
            image = image.model_copy(update={"monitor_id": monitor_id})
        return image

    # -- Windows ---------------------------------------------------------
    async def list_windows(self) -> list[WindowInfo]:
        if not self.available:
            return []
        return await self._call(self._adapter.list_windows)

    async def get_active_window(self) -> WindowInfo | None:
        for window in await self.list_windows():
            if window.active:
                return window
        return None

    async def focus_window(self, window_id: str) -> bool:
        self.require_available()
        return await self._call(self._adapter.focus_window, window_id)

    # -- Input -----------------------------------------------------------
    async def pointer_move(self, point: Point) -> None:
        self.require_available()
        monitors = await self._call(self._monitors)
        physical = self._physical(point, monitors)
        await self._call(self._adapter.pointer_move, physical.x, physical.y)

    async def pointer_click(
        self, point: Point, *, button: PointerButton = PointerButton.LEFT, double: bool = False
    ) -> None:
        self.require_available()
        monitors = await self._call(self._monitors)
        physical = self._physical(point, monitors)
        await self._call(self._adapter.pointer_click, physical.x, physical.y, button, double)

    async def pointer_scroll(self, *, amount: int, point: Point | None = None) -> None:
        self.require_available()
        if point is not None:
            monitors = await self._call(self._monitors)
            physical = self._physical(point, monitors)
            await self._call(self._adapter.pointer_move, physical.x, physical.y)
        await self._call(self._adapter.pointer_scroll, amount)

    async def keyboard_type(self, text: str) -> None:
        self.require_available()
        await self._call(self._adapter.type_text, text)

    async def keyboard_key(self, key: str) -> None:
        self.require_available()
        await self._call(self._adapter.press_key, key)

    async def keyboard_hotkey(self, keys: list[str]) -> None:
        self.require_available()
        await self._call(self._adapter.hotkey, keys)

    async def aclose(self) -> None:
        return None


__all__ = ["MAX_WINDOWS", "CtypesWin32Adapter", "Win32Adapter", "WindowsBackend"]
