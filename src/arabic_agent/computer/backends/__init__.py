"""Computer backend selection.

One place chooses the backend for the current machine, mirroring how
``local/adapters.py`` chooses platform adapters. The Linux/X11 and Windows
(Win32) backends are implemented; macOS reports itself unavailable with a clear
reason rather than pretending. A backend that is not implemented, or is
implemented but cannot run here, is returned as an honest null backend carrying
the reason, so callers never mistake "not available" for "no backend".
"""

from __future__ import annotations

from ...core.enums import PlatformKind
from ..base import ComputerBackend
from .linux_x11 import X11Backend
from .null import NullComputerBackend
from .windows import WindowsBackend


def build_computer_backend(
    *, platform: PlatformKind | None = None, display: str | None = None
) -> ComputerBackend:
    """Return the best available backend for ``platform``.

    An unavailable backend is returned (not ``None``) when nothing is
    implemented, so callers can inspect ``unavailable_reason`` and report it
    honestly.
    """
    from ...local.adapters import detect_platform

    kind = platform or detect_platform()
    if kind is PlatformKind.LINUX:
        backend = X11Backend(display=display)
        if backend.available:
            return backend
        return NullComputerBackend(backend.unavailable_reason)
    if kind is PlatformKind.WINDOWS:
        windows = WindowsBackend()
        if windows.available:
            return windows
        return NullComputerBackend(windows.unavailable_reason)
    if kind is PlatformKind.MACOS:
        return NullComputerBackend(
            "the macOS computer backend is not implemented yet (design only)"
        )
    return NullComputerBackend()


__all__ = ["build_computer_backend"]
