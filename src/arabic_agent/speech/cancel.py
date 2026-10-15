"""Cooperative cancellation for long-running speech operations.

Recording ("stop when I say so") and playback ("stop talking") are the two
operations a voice user must be able to interrupt. Both are a blocking loop over
audio hardware, so they cannot be killed from outside — they have to *ask*
whether they should stop. This module is that ask, in one place.

A :class:`CancelToken` is a tiny, thread-safe flag. The desktop voice service
creates one per operation, hands a ``should_cancel`` callable down through the
runtime to the audio backend's polling loop, and cancels the token when the user
presses stop. The loop returns early and the operation raises
:class:`CancelledError`, which the API surfaces as a cancelled result rather than
a failure.

It is deliberately not an :class:`asyncio.Event`: the polling loops run inside
``asyncio.to_thread`` on the audio path, and a plain lock-protected boolean is
safe to read from that thread without touching the event loop.
"""

from __future__ import annotations

import threading

from ..core.enums import ErrorClass
from ..core.errors import PlatformError


class CancelledError(PlatformError):
    """The operation was cancelled on request.

    Classified so the agent loop (and any future workflow step) treats a
    cancellation as a terminal, non-retryable outcome: it is a deliberate stop,
    not a transient fault, and retrying it would be wrong.
    """

    error_class = ErrorClass.CANCELLED
    retryable = False


class CancelToken:
    """A thread-safe cancellation flag shared with a blocking audio loop."""

    __slots__ = ("_cancelled", "_lock", "_reason")

    def __init__(self, *, reason: str = "") -> None:
        self._cancelled = False
        self._lock = threading.Lock()
        self._reason = reason

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def cancel(self, reason: str = "") -> None:
        """Request cancellation; idempotent and safe to call from any thread."""
        with self._lock:
            self._cancelled = True
            if reason:
                self._reason = reason

    def should_cancel(self) -> bool:
        """The callable the audio loops poll (bound method, no allocation)."""
        return self.cancelled

    def raise_if_cancelled(self) -> None:
        """Raise :class:`CancelledError` when cancellation was requested."""
        if self.cancelled:
            raise CancelledError(self.reason or "the operation was cancelled")


__all__ = ["CancelToken", "CancelledError"]
