"""Print backend selection.

One place chooses the backend for the current machine, mirroring how
``computer/backends`` chooses a computer backend. The CUPS (POSIX) and Windows
spooler backends are implemented; macOS reports itself unavailable with a clear
reason rather than pretending. A backend that is not implemented, or is
implemented but cannot run here, is returned as an honest null backend carrying
the reason, so callers never mistake "not available" for "no backend".
"""

from __future__ import annotations

from ...core.enums import PlatformKind
from ..base import PrintBackend
from .cups import CupsBackend
from .null import NullPrintBackend
from .windows import WindowsPrintBackend


def build_print_backend(*, platform: PlatformKind | None = None) -> PrintBackend:
    """Return the best available backend for ``platform``.

    An unavailable backend is returned (not ``None``) when nothing is
    implemented, so callers can inspect ``unavailable_reason`` and report it
    honestly.
    """
    from ...local.adapters import detect_platform

    kind = platform or detect_platform()
    if kind is PlatformKind.LINUX:
        cups = CupsBackend()
        if cups.available:
            return cups
        return NullPrintBackend(cups.unavailable_reason)
    if kind is PlatformKind.WINDOWS:
        windows = WindowsPrintBackend()
        if windows.available:
            return windows
        return NullPrintBackend(windows.unavailable_reason)
    if kind is PlatformKind.MACOS:
        return NullPrintBackend("the macOS printing backend is not implemented yet (design only)")
    return NullPrintBackend()


__all__ = ["build_print_backend"]
