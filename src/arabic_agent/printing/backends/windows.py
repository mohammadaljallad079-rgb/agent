"""Windows printing backend (Win32 spooler).

The Windows half of the printing seam. It implements the same
:class:`~arabic_agent.printing.base.PrintBackend` contract as the CUPS backend,
so the policy, budget and audit above it are unchanged.

Two layers, deliberately separated:

* :class:`WinSpoolAdapter` is the *only* place that touches the Windows spooler
  API. It is a narrow, injectable seam: every method returns plain values, so a
  test can substitute a fake adapter to exercise the whole backend off Windows.
* :class:`WindowsPrintBackend` translates those raw values into the shared
  models and reports availability honestly.

The real adapter uses ``ctypes`` and the Win32 spooler API
(``EnumPrintersW``/``GetDefaultPrinterW`` to enumerate, ``OpenPrinterW`` +
``StartDocPrinterW``/``WritePrinter`` to submit, ``SetJobW`` to cancel) and
sends the file as **raw** spool data (``DATATYPE "RAW"``), so any format the
printer's own driver understands — including PDF on a modern driver — is passed
through unchanged rather than being re-rendered. Because ``ctypes.windll`` only
exists on Windows, the module imports and the class constructs on any OS: off
Windows the adapter reports itself unavailable with a clear reason.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ...core.enums import PlatformKind
from ...core.errors import ExternalServiceError, NotFoundError
from ..base import (
    PrintBackend,
    PrintColorMode,
    PrintDocument,
    PrintDuplex,
    PrinterInfo,
    PrintJob,
    PrintJobStatus,
    PrintOptions,
)

logger = logging.getLogger(__name__)

#: Largest chunk written to the spooler at once (1 MiB).
_CHUNK = 1024 * 1024


class WinSpoolAdapter(ABC):
    """The narrow seam over the Win32 spooler API."""

    @abstractmethod
    def list_printers(self) -> list[dict[str, Any]]:
        """Return printer records with ``name`` and ``default`` keys."""

    @abstractmethod
    def submit_raw(self, *, printer: str, title: str, path: str) -> str:
        """Spool ``path`` as a RAW job; return the new job id."""

    @abstractmethod
    def cancel(self, *, printer: str, job_id: str) -> bool:
        """Cancel a job; return whether the spooler accepted it."""


class CtypesWinSpoolAdapter(WinSpoolAdapter):
    """The real adapter, implemented over ``ctypes`` and the Win32 spooler API."""

    def __init__(self) -> None:
        import ctypes

        self._load = getattr(ctypes, "WinDLL", None)
        self._available = self._load is not None
        self.unavailable_reason = (
            "" if self._available else "the Win32 spooler API is only available on Windows"
        )

    def _spooler(self) -> Any:  # pragma: no cover - Windows only
        assert self._load is not None
        return self._load("winspool.drv")

    # -- Enumerate -------------------------------------------------------
    def list_printers(self) -> list[dict[str, Any]]:
        if not self._available:
            return []
        return self._list_printers_win32()

    def _list_printers_win32(self) -> list[dict[str, Any]]:  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes

        winspool = self._spooler()
        PRINTER_ENUM_LOCAL = 0x00000002
        PRINTER_ENUM_CONNECTIONS = 0x00000004
        flags = PRINTER_ENUM_LOCAL | PRINTER_ENUM_CONNECTIONS
        needed = wintypes.DWORD(0)
        returned = wintypes.DWORD(0)
        winspool.EnumPrintersW(
            flags, None, 2, None, 0, ctypes.byref(needed), ctypes.byref(returned)
        )
        if needed.value == 0:
            return []
        buffer = ctypes.create_string_buffer(needed.value)

        class PRINTER_INFO_2(ctypes.Structure):
            _fields_ = [
                ("pServerName", wintypes.LPWSTR),
                ("pPrinterName", wintypes.LPWSTR),
                ("pShareName", wintypes.LPWSTR),
                ("pPortName", wintypes.LPWSTR),
                ("pDriverName", wintypes.LPWSTR),
                ("pComment", wintypes.LPWSTR),
                ("pLocation", wintypes.LPWSTR),
                ("pDevMode", ctypes.c_void_p),
                ("pSepFile", wintypes.LPWSTR),
                ("pPrintProcessor", wintypes.LPWSTR),
                ("pDatatype", wintypes.LPWSTR),
                ("pParameters", wintypes.LPWSTR),
                ("pSecurityDescriptor", ctypes.c_void_p),
                ("Attributes", wintypes.DWORD),
                ("Priority", wintypes.DWORD),
                ("DefaultPriority", wintypes.DWORD),
                ("StartTime", wintypes.DWORD),
                ("UntilTime", wintypes.DWORD),
                ("Status", wintypes.DWORD),
                ("cJobs", wintypes.DWORD),
                ("AveragePPM", wintypes.DWORD),
            ]

        if not winspool.EnumPrintersW(
            flags,
            None,
            2,
            buffer,
            needed.value,
            ctypes.byref(needed),
            ctypes.byref(returned),
        ):
            return []
        array = ctypes.cast(buffer, ctypes.POINTER(PRINTER_INFO_2))
        default = self._default_printer_win32(winspool)
        records: list[dict[str, Any]] = []
        for i in range(returned.value):
            info = array[i]
            name = info.pPrinterName or ""
            status = int(info.Status)
            # PRINTER_STATUS_* bit 0x00000080 is offline; 0x00100000 is error.
            available = status & 0x00000080 == 0
            records.append(
                {
                    "name": name,
                    "default": name == default,
                    "available": available,
                    "detail": info.pLocation or "",
                }
            )
        return records

    def _default_printer_win32(self, winspool: Any) -> str | None:  # pragma: no cover
        import ctypes
        from ctypes import wintypes

        size = wintypes.DWORD(0)
        winspool.GetDefaultPrinterW(None, ctypes.byref(size))
        if size.value == 0:
            return None
        buffer = ctypes.create_unicode_buffer(size.value)
        if winspool.GetDefaultPrinterW(buffer, ctypes.byref(size)):
            return buffer.value
        return None

    # -- Submit ----------------------------------------------------------
    def submit_raw(self, *, printer: str, title: str, path: str) -> str:
        if not self._available:
            raise ExternalServiceError("the Win32 spooler API is only available on Windows")
        return self._submit_raw_win32(printer=printer, title=title, path=path)

    def _submit_raw_win32(  # pragma: no cover - Windows only
        self, *, printer: str, title: str, path: str
    ) -> str:
        import ctypes
        from ctypes import wintypes

        winspool = self._spooler()

        class DOC_INFO_1(ctypes.Structure):
            _fields_ = [
                ("pDocName", wintypes.LPWSTR),
                ("pOutputFile", wintypes.LPWSTR),
                ("pDatatype", wintypes.LPWSTR),
            ]

        handle = wintypes.HANDLE()
        if not winspool.OpenPrinterW(printer, ctypes.byref(handle), None):
            raise ExternalServiceError(
                "could not open the printer", details={"printer": printer}
            )
        try:
            doc = DOC_INFO_1(title, None, "RAW")
            job_id = winspool.StartDocPrinterW(handle, 1, ctypes.byref(doc))
            if not job_id:
                raise ExternalServiceError("the spooler refused to start the document")
            winspool.StartPagePrinter(handle)
            try:
                with open(path, "rb") as handle_file:
                    while True:
                        chunk = handle_file.read(_CHUNK)
                        if not chunk:
                            break
                        buf = ctypes.create_string_buffer(chunk, len(chunk))
                        written = wintypes.DWORD(0)
                        if not winspool.WritePrinter(
                            handle, buf, len(chunk), ctypes.byref(written)
                        ):
                            raise ExternalServiceError("writing to the spooler failed")
            finally:
                winspool.EndPagePrinter(handle)
                winspool.EndDocPrinter(handle)
            return str(job_id)
        finally:
            winspool.ClosePrinter(handle)

    # -- Cancel ----------------------------------------------------------
    def cancel(self, *, printer: str, job_id: str) -> bool:
        if not self._available:
            return False
        return self._cancel_win32(printer=printer, job_id=job_id)

    def _cancel_win32(self, *, printer: str, job_id: str) -> bool:  # pragma: no cover
        import ctypes
        from ctypes import wintypes

        winspool = self._spooler()
        handle = wintypes.HANDLE()
        if not winspool.OpenPrinterW(printer, ctypes.byref(handle), None):
            return False
        try:
            JOB_CONTROL_CANCEL = 3
            return bool(winspool.SetJobW(handle, int(job_id), 0, None, JOB_CONTROL_CANCEL))
        finally:
            winspool.ClosePrinter(handle)


class WindowsPrintBackend(PrintBackend):
    """A real Windows printing backend, available only on Windows."""

    name = "windows"
    platform = PlatformKind.WINDOWS

    def __init__(self, *, adapter: WinSpoolAdapter | None = None) -> None:
        self._adapter = adapter or CtypesWinSpoolAdapter()
        available = getattr(self._adapter, "_available", True) if adapter is None else True
        self.available = bool(available)
        self.unavailable_reason = (
            "" if self.available else getattr(self._adapter, "unavailable_reason", "unavailable")
        )

    async def list_printers(self) -> list[PrinterInfo]:
        self.require_available()
        records = await asyncio.to_thread(self._adapter.list_printers)
        return [
            PrinterInfo(
                id=str(record.get("name", "")),
                name=str(record.get("name", "")),
                available=bool(record.get("available", True)),
                accepting_jobs=True,
                is_default=bool(record.get("default", False)),
                backend=self.name,
                detail=str(record.get("detail", "")),
            )
            for record in records
            if record.get("name")
        ]

    async def submit(
        self, *, document: PrintDocument, printer_id: str, options: PrintOptions
    ) -> PrintJob:
        self.require_available()
        path = Path(document.path)
        if not await asyncio.to_thread(path.is_file):
            raise NotFoundError(
                "document to print was not found", details={"path": document.path}
            )
        title = document.title or path.name
        # The RAW datatype hands the file to the driver untouched; options that
        # need driver rendering (duplex/media/copies) are a documented limit of
        # the raw path and are not silently pretended to be applied.
        job_id = await asyncio.to_thread(
            self._adapter.submit_raw, printer=printer_id, title=title, path=str(path)
        )
        return PrintJob(
            id=job_id,
            printer_id=printer_id,
            title=title,
            status=PrintJobStatus.QUEUED,
            detail="submitted as RAW spool data",
            backend=self.name,
        )

    async def cancel_job(self, job_id: str, *, printer_id: str = "") -> bool:
        if not self.available:
            return False
        printer = printer_id
        if not printer:
            return False
        return await asyncio.to_thread(self._adapter.cancel, printer=printer, job_id=job_id)


# Colour/duplex are intentionally unused by the raw path; referenced so the
# imports document the full option vocabulary the interface accepts.
_ = (PrintColorMode, PrintDuplex)

__all__ = ["CtypesWinSpoolAdapter", "WinSpoolAdapter", "WindowsPrintBackend"]
