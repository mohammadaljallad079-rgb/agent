"""Linux/X11 computer backend.

This is a *real* backend: it captures the actual X display and synthesises real
input through the standard X11 tooling (``xdotool`` for input and window
management, ``xrandr`` for the monitor layout, ``Pillow`` for capture). It is the
backend used by the offline/desktop runtime on Linux.

Every external call is made with ``shell=False`` and an explicit argv, so no
value the model supplies can become a second command. The display is taken from
the process environment (``DISPLAY``) and is never user-supplied. Failures are
surfaced as classified platform errors, not swallowed: a missing ``xdotool`` or
an unreachable display makes the backend report itself unavailable rather than
pretending to work.

Coordinates from this backend are **physical** pixels: on X11 the pointer
coordinates ``xdotool`` uses are the same device pixels ``Pillow`` captures, so
the logical→physical transform is the identity (``scale = 1.0``). A DPI scale is
still modelled so a scaled session is handled correctly if one is configured.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path

from ...core.enums import PlatformKind
from ...core.errors import ExternalServiceError
from ..base import CapturedImage, ComputerBackend, PointerButton, ScreenMetadata, WindowInfo
from ..capture import encode_png
from ..coords import CoordinateSpace, MonitorInfo, Point, Rect

_MONITOR_RE = re.compile(
    r"^\s*(?P<id>\d+):\s+\+(?P<name>\S+)\s+"
    r"(?P<w>\d+)/(?:\d+)x(?P<h>\d+)/(?:\d+)\+(?P<x>-?\d+)\+(?P<y>-?\d+)"
)
_CURSOR_RE = re.compile(r"x:(?P<x>-?\d+)\s+y:(?P<y>-?\d+)")
_GEOMETRY_RE = re.compile(r"(?P<w>\d+)x(?P<h>\d+)\+(?P<x>-?\d+)\+(?P<y>-?\d+)")

#: Cap on windows returned, so a busy session cannot flood the model.
MAX_WINDOWS = 50


class X11Backend(ComputerBackend):
    """Real X11 capture and input."""

    name = "x11"
    platform = PlatformKind.LINUX
    supports_windows = True

    def __init__(self, *, display: str | None = None, timeout_seconds: float = 10.0) -> None:
        self._display = display or os.environ.get("DISPLAY", "")
        self._timeout = timeout_seconds
        self._xdotool = shutil.which("xdotool") or ""
        self._xrandr = shutil.which("xrandr") or ""
        self._has_pillow = self._probe_pillow()
        self._display_reachable = self._probe_display(self._display)
        self.available = bool(
            self._display
            and self._display_reachable
            and self._xdotool
            and self._xrandr
            and self._has_pillow
        )
        if not self.available:
            reasons = []
            if not self._display:
                reasons.append("DISPLAY is not set")
            elif not self._display_reachable:
                reasons.append(f"display {self._display} is not reachable")
            if not self._xdotool:
                reasons.append("xdotool not found")
            if not self._xrandr:
                reasons.append("xrandr not found")
            if not self._has_pillow:
                reasons.append("Pillow not installed")
            self.unavailable_reason = "x11 backend unavailable: " + "; ".join(reasons)

    @staticmethod
    def _probe_display(display: str) -> bool:
        """Whether an X display string points at a reachable local server.

        A local ``:N`` display is checked by its unix socket, so an
        unavailable/stale ``DISPLAY`` is reported honestly at construction
        instead of failing on the first capture. A remote ``host:N`` display
        cannot be probed cheaply and is verified on first use.
        """
        if not display:
            return False
        match = re.match(r"^(?P<host>[^:]*):(?P<num>\d+)(?:\.\d+)?$", display)
        if match is None:
            return True
        if match.group("host"):
            return True
        # The well-known X11 socket directory; a fixed, non-temporary path.
        socket = Path("/tmp/.X11-unix") / f"X{match.group('num')}"  # noqa: S108
        return socket.exists()

    @staticmethod
    def _probe_pillow() -> bool:
        try:
            import PIL.ImageGrab  # noqa: F401

            return True
        except ImportError:
            return False

    def _env(self) -> dict[str, str]:
        env = {
            k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TZ") if k in os.environ
        }
        env["DISPLAY"] = self._display
        return env

    async def _run(self, argv: list[str], *, input_bytes: bytes | None = None) -> str:
        self.require_available()
        process = await asyncio.create_subprocess_exec(
            *argv,
            env=self._env(),
            stdin=asyncio.subprocess.PIPE
            if input_bytes is not None
            else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=input_bytes), timeout=self._timeout
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ExternalServiceError(f"{argv[0]} timed out") from exc
        if process.returncode != 0:
            raise ExternalServiceError(
                f"{os.path.basename(argv[0])} failed",
                details={"stderr": stderr.decode("utf-8", errors="replace")[:200]},
            )
        return stdout.decode("utf-8", errors="replace")

    # -- Perception ------------------------------------------------------
    async def _monitors(self) -> list[MonitorInfo]:
        output = await self._run([self._xrandr, "--listmonitors"])
        monitors: list[MonitorInfo] = []
        for line in output.splitlines():
            match = _MONITOR_RE.match(line)
            if not match:
                continue
            monitors.append(
                MonitorInfo(
                    id=int(match.group("id")),
                    name=match.group("name"),
                    x=int(match.group("x")),
                    y=int(match.group("y")),
                    width=int(match.group("w")),
                    height=int(match.group("h")),
                    primary=not monitors,
                )
            )
        return monitors

    async def get_screen_metadata(self) -> ScreenMetadata:
        monitors = await self._monitors()
        cursor: Point | None = None
        try:
            location = await self._run([self._xdotool, "getmouselocation"])
            match = _CURSOR_RE.search(location)
            if match:
                cursor = Point(
                    x=int(match.group("x")),
                    y=int(match.group("y")),
                    space=CoordinateSpace.PHYSICAL,
                )
        except ExternalServiceError:
            cursor = None
        from ..coords import virtual_bounds

        return ScreenMetadata(
            monitors=tuple(monitors),
            virtual_bounds=virtual_bounds(monitors) if monitors else None,
            cursor=cursor,
            platform=self.platform,
            backend=self.name,
            notes=() if monitors else ("no monitors reported by xrandr",),
        )

    async def capture_screen(self, *, monitor_id: int | None = None) -> CapturedImage:
        self.require_available()
        import time

        from PIL import ImageGrab

        monitors = await self._monitors()
        region: Rect | None = None
        if monitor_id is not None:
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
        try:
            image = ImageGrab.grab(
                xdisplay=self._display,
                bbox=None if region is None else (region.x, region.y, region.right, region.bottom),
            )
        except Exception as exc:  # capture can fail if the display vanished
            raise ExternalServiceError(f"screen capture failed: {exc}") from exc
        return CapturedImage(
            data=encode_png(image),
            width=image.width,
            height=image.height,
            monitor_id=monitor_id,
            region=region,
            space=CoordinateSpace.PHYSICAL,
            captured_at=time.monotonic(),
        )

    # -- Windows ---------------------------------------------------------
    async def list_windows(self) -> list[WindowInfo]:
        try:
            ids_output = await self._run([self._xdotool, "search", "--onlyvisible", "--name", ".*"])
        except ExternalServiceError:
            return []
        active_id = ""
        try:
            active_id = (await self._run([self._xdotool, "getactivewindow"])).strip()
        except ExternalServiceError:
            active_id = ""
        windows: list[WindowInfo] = []
        for raw in ids_output.split()[:MAX_WINDOWS]:
            try:
                title = (await self._run([self._xdotool, "getwindowname", raw])).strip()
            except ExternalServiceError:
                title = ""
            try:
                geometry = await self._run([self._xdotool, "getwindowgeometry", "--shell", raw])
            except ExternalServiceError:
                continue
            values: dict[str, int] = {}
            for line in geometry.splitlines():
                if "=" in line:
                    key, _, value = line.partition("=")
                    if value.strip().lstrip("-").isdigit():
                        values[key.strip()] = int(value.strip())
            windows.append(
                WindowInfo(
                    id=raw,
                    title=title,
                    x=values.get("X", 0),
                    y=values.get("Y", 0),
                    width=values.get("WIDTH", 0),
                    height=values.get("HEIGHT", 0),
                    active=raw == active_id,
                    visible=True,
                )
            )
        return windows

    async def get_active_window(self) -> WindowInfo | None:
        try:
            active_id = (await self._run([self._xdotool, "getactivewindow"])).strip()
        except ExternalServiceError:
            return None
        for window in await self.list_windows():
            if window.id == active_id:
                return window
        return WindowInfo(id=active_id, active=True)

    async def focus_window(self, window_id: str) -> bool:
        try:
            await self._run([self._xdotool, "windowactivate", "--sync", window_id])
            return True
        except ExternalServiceError:
            return False

    # -- Input -----------------------------------------------------------
    async def pointer_move(self, point: Point) -> None:
        await self._run([self._xdotool, "mousemove", str(point.x), str(point.y)])

    async def pointer_click(
        self, point: Point, *, button: PointerButton = PointerButton.LEFT, double: bool = False
    ) -> None:
        button_number = {
            PointerButton.LEFT: "1",
            PointerButton.MIDDLE: "2",
            PointerButton.RIGHT: "3",
        }[button]
        argv = [
            self._xdotool,
            "mousemove",
            str(point.x),
            str(point.y),
            "click",
            "--repeat",
            "2" if double else "1",
            "--delay",
            "120",
            button_number,
        ]
        await self._run(argv)

    async def pointer_scroll(self, *, amount: int, point: Point | None = None) -> None:
        # X11 buttons 4 (up) and 5 (down); xdotool has no native scroll, so emit
        # discrete wheel clicks. Bound the count so one action cannot spin.
        clicks = min(abs(amount), 100)
        button = "4" if amount > 0 else "5"
        argv = [self._xdotool]
        if point is not None:
            argv += ["mousemove", str(point.x), str(point.y)]
        argv += ["click", "--repeat", str(clicks), "--delay", "20", button]
        await self._run(argv)

    async def keyboard_type(self, text: str) -> None:
        await self._run([self._xdotool, "type", "--clearmodifiers", "--delay", "12", "--", text])

    async def keyboard_key(self, key: str) -> None:
        await self._run([self._xdotool, "key", "--clearmodifiers", key])

    async def keyboard_hotkey(self, keys: list[str]) -> None:
        chord = "+".join(keys)
        await self._run([self._xdotool, "key", "--clearmodifiers", chord])


__all__ = ["MAX_WINDOWS", "X11Backend"]
