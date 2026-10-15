"""Audio backend selection.

One place chooses the backend for the current machine, mirroring how
``printing/backends`` chooses a print backend. The POSIX (ALSA) and Windows
(WinMM) backends are implemented; macOS reports itself unavailable with a clear
reason rather than pretending. A backend that is not implemented, or is
implemented but cannot run here, is returned as an honest null backend carrying
the reason, so callers never mistake "not available" for "no backend".
"""

from __future__ import annotations

from ...core.enums import PlatformKind
from ..base import AudioBackend
from .null import NullAudioBackend
from .posix import PosixAudioBackend
from .windows import WindowsAudioBackend


def build_audio_backend(*, platform: PlatformKind | None = None) -> AudioBackend:
    """Return the best available backend for ``platform``.

    An unavailable backend is returned (not ``None``) when nothing is
    implemented, so callers can inspect ``unavailable_reason`` and report it
    honestly.
    """
    from ...local.adapters import detect_platform

    kind = platform or detect_platform()
    if kind is PlatformKind.LINUX:
        posix = PosixAudioBackend()
        if posix.available:
            return posix
        return NullAudioBackend(posix.unavailable_reason)
    if kind is PlatformKind.WINDOWS:
        windows = WindowsAudioBackend()
        if windows.available:
            return windows
        return NullAudioBackend(windows.unavailable_reason)
    if kind is PlatformKind.MACOS:
        return NullAudioBackend("the macOS audio backend is not implemented yet (design only)")
    return NullAudioBackend()


__all__ = ["build_audio_backend"]
