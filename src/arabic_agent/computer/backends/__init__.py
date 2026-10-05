"""Computer backend selection.

One place chooses the backend for the current machine, mirroring how
``local/adapters.py`` chooses platform adapters. Only the Linux/X11 backend is
implemented today; Windows and macOS report themselves unavailable with a clear
reason rather than pretending. The design for those platforms is described in
``ARCHITECTURE.md``; the interfaces they must implement already exist.
"""

from __future__ import annotations

from ...core.enums import PlatformKind
from ..base import ComputerBackend
from .linux_x11 import X11Backend
from .null import NullComputerBackend


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
        return NullComputerBackend(
            "the Windows computer backend is not implemented yet (design only)"
        )
    if kind is PlatformKind.MACOS:
        return NullComputerBackend(
            "the macOS computer backend is not implemented yet (design only)"
        )
    return NullComputerBackend()


__all__ = ["build_computer_backend"]
