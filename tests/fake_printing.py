"""A scripted printing backend for tests.

It performs no spooler I/O but records every call and returns realistic values,
so the runtime, policy, boundary, tools and executor can be exercised without a
printer. It is deliberately honest about being a test double: it is only ever
injected explicitly, never selected by :func:`build_print_backend`.
"""

from __future__ import annotations

from arabic_agent.core.enums import PlatformKind
from arabic_agent.printing.base import (
    PrintBackend,
    PrintDocument,
    PrinterCapabilities,
    PrinterInfo,
    PrintJob,
    PrintJobStatus,
    PrintOptions,
)


class FakePrintBackend(PrintBackend):
    name = "fake"
    platform = PlatformKind.LINUX
    available = True

    def __init__(
        self,
        *,
        printers: list[PrinterInfo] | None = None,
        fail_submit: bool = False,
        cancel_result: bool = True,
        status: PrintJobStatus = PrintJobStatus.PRINTING,
        capabilities: PrinterCapabilities | None = None,
    ) -> None:
        self._printers = printers or [
            PrinterInfo(id="HP_LaserJet", name="HP LaserJet", is_default=True, backend=self.name),
            PrinterInfo(id="Office_Color", name="Office Colour", backend=self.name),
        ]
        self._fail_submit = fail_submit
        self._cancel_result = cancel_result
        self._status = status
        self._capabilities = capabilities
        self.submitted: list[tuple[PrintDocument, str, PrintOptions]] = []
        self.cancelled: list[tuple[str, str]] = []
        self.status_queries: list[tuple[str, str]] = []

    async def list_printers(self) -> list[PrinterInfo]:
        self.require_available()
        return list(self._printers)

    async def submit(
        self, *, document: PrintDocument, printer_id: str, options: PrintOptions
    ) -> PrintJob:
        self.require_available()
        if self._fail_submit:
            from arabic_agent.core.errors import ExternalServiceError

            raise ExternalServiceError("the spooler rejected the job")
        self.submitted.append((document, printer_id, options))
        return PrintJob(
            id=f"{printer_id}-{len(self.submitted)}",
            printer_id=printer_id,
            title=document.title,
            status=PrintJobStatus.QUEUED,
            backend=self.name,
        )

    async def job_status(self, job_id: str, *, printer_id: str = "") -> PrintJobStatus:
        self.status_queries.append((job_id, printer_id))
        return self._status

    async def cancel_job(self, job_id: str, *, printer_id: str = "") -> bool:
        self.cancelled.append((job_id, printer_id))
        return self._cancel_result

    async def printer_capabilities(self, printer_id: str) -> PrinterCapabilities | None:
        return self._capabilities


class FakeUnavailablePrintBackend(PrintBackend):
    name = "none"
    platform = PlatformKind.UNKNOWN
    available = False
    unavailable_reason = "no printing backend is implemented for this platform"

    async def list_printers(self) -> list[PrinterInfo]:
        self.require_available()
        return []

    async def submit(
        self, *, document: PrintDocument, printer_id: str, options: PrintOptions
    ) -> PrintJob:
        self.require_available()
        raise AssertionError("unreachable")


__all__ = ["FakePrintBackend", "FakeUnavailablePrintBackend"]
