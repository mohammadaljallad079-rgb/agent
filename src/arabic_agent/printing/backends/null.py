"""A printing backend that is explicitly not available.

Used on platforms where no backend is implemented yet (macOS) and in tests that
need the *unavailable* path. It reports why it is unavailable so the print tools
fail honestly instead of pretending to print.
"""

from __future__ import annotations

from ...core.enums import PlatformKind
from ..base import PrintBackend, PrintDocument, PrinterInfo, PrintJob, PrintOptions


class NullPrintBackend(PrintBackend):
    """An unavailable backend; every operation refuses with a clear reason."""

    name = "none"
    platform = PlatformKind.UNKNOWN
    available = False

    def __init__(
        self, reason: str = "no printing backend is implemented for this platform"
    ) -> None:
        self.unavailable_reason = reason

    async def list_printers(self) -> list[PrinterInfo]:
        self.require_available()
        return []

    async def submit(
        self, *, document: PrintDocument, printer_id: str, options: PrintOptions
    ) -> PrintJob:
        self.require_available()
        raise AssertionError("unreachable")


__all__ = ["NullPrintBackend"]
