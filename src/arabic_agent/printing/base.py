"""Printing abstraction.

The agent must never see a raw OS printing API. A :class:`PrintBackend` is the
single seam between the agent's print tools and whatever actually submits a job
— CUPS here, the Windows spooler later. The interface is intentionally small:

    list_printers()      submit(document, options)      job_status(job_id)
    cancel_job(job_id)

A backend declares whether it is *available* on the current machine and, if not,
*why* — an unavailable backend refuses with a clear reason instead of pretending
to print. The concrete adapters live in
:mod:`arabic_agent.printing.backends`.

Everything a backend returns is a plain, serialisable model so it can cross the
tool boundary and be redacted and audited. A document is passed as a
:class:`PrintDocument` that names a *file inside the sandbox*; the backend
resolves and reads it — no raw bytes from the model are ever spooled.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import PlatformKind
from ..core.errors import ConfigurationError


class PrintColorMode(StrEnum):
    """Colour handling for a job."""

    COLOR = "color"
    MONOCHROME = "monochrome"


class PrintDuplex(StrEnum):
    """Two-sided printing mode."""

    ONE_SIDED = "one_sided"
    LONG_EDGE = "long_edge"
    SHORT_EDGE = "short_edge"


class PrinterInfo(BaseModel):
    """One printer known to the host spooler."""

    model_config = ConfigDict(frozen=True)

    #: Backend-stable identifier (the queue name on CUPS, the printer name on
    #: Windows). This is what a print request targets.
    id: str
    #: Human-facing name, falling back to the id.
    name: str = ""
    #: Whether the spooler reports the printer as usable right now.
    available: bool = True
    #: Whether the printer accepts jobs (not paused/stopped).
    accepting_jobs: bool = True
    #: Whether this is the spooler's default printer.
    is_default: bool = False
    #: Backend that discovered it (``"cups"``, ``"windows"``, ...).
    backend: str = ""
    #: Human-readable detail (location, status message), if any.
    detail: str = ""

    @property
    def display_name(self) -> str:
        return self.name or self.id


class PrintOptions(BaseModel):
    """Bounded, validated job options. Unknown/extra options are refused."""

    model_config = ConfigDict(frozen=True)

    copies: int = Field(default=1, ge=1, le=99)
    color: PrintColorMode = PrintColorMode.COLOR
    duplex: PrintDuplex = PrintDuplex.ONE_SIDED
    #: Media/paper size, e.g. ``A4`` or ``Letter``. ``None`` uses the printer's
    #: own default rather than guessing one.
    media: str | None = None
    #: Page range as a spooler expression (e.g. ``"1-3"``). ``None`` = all pages.
    page_range: str | None = None


class PrintDocument(BaseModel):
    """A document to print, referenced by an in-sandbox file path.

    The document is *not* carried as bytes: the backend reads the resolved file
    so the model cannot inject arbitrary content into a spooler. ``title`` is
    the job name shown in the spooler.
    """

    model_config = ConfigDict(frozen=True)

    #: Absolute, sandbox-resolved path to the file to print.
    path: str
    #: Job title (defaults to the file name).
    title: str = ""
    #: MIME type hint, when known (``application/pdf`` etc.).
    content_type: str = ""


class PrintJobStatus(StrEnum):
    """State of a submitted job, as far as the spooler reports it."""

    QUEUED = "queued"
    PRINTING = "printing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    UNKNOWN = "unknown"


class PrintJob(BaseModel):
    """A submitted print job."""

    model_config = ConfigDict(frozen=True)

    id: str
    printer_id: str
    title: str = ""
    status: PrintJobStatus = PrintJobStatus.QUEUED
    detail: str = ""
    #: Backend that accepted the job.
    backend: str = ""


class PrintBackend(ABC):
    """Contract every platform printing backend implements.

    Implementations perform real spooler operations. Permission, approval,
    policy, rate-limit and audit layers live *above* this interface and are
    never the backend's responsibility.
    """

    #: Stable identifier used in configuration and audit (e.g. ``"cups"``).
    name: str
    #: The OS family this backend targets.
    platform: PlatformKind = PlatformKind.UNKNOWN
    #: Whether this backend can actually run on the current machine.
    available: bool = False
    #: Why it is unavailable, when :attr:`available` is ``False``.
    unavailable_reason: str = "not implemented"

    def require_available(self) -> None:
        if not self.available:
            raise ConfigurationError(
                f"{type(self).__name__} is unavailable: {self.unavailable_reason}"
            )

    @abstractmethod
    async def list_printers(self) -> list[PrinterInfo]:
        """Return the printers the host spooler knows about."""

    @abstractmethod
    async def submit(
        self, *, document: PrintDocument, printer_id: str, options: PrintOptions
    ) -> PrintJob:
        """Submit ``document`` to ``printer_id`` and return the created job."""

    async def job_status(self, job_id: str) -> PrintJobStatus:
        """Return the current status of a job, if the backend can report it."""
        return PrintJobStatus.UNKNOWN

    async def cancel_job(self, job_id: str, *, printer_id: str = "") -> bool:
        """Cancel a job; return whether the spooler accepted the cancellation."""
        return False


__all__ = [
    "PrintBackend",
    "PrintColorMode",
    "PrintDocument",
    "PrintDuplex",
    "PrintJob",
    "PrintJobStatus",
    "PrintOptions",
    "PrinterInfo",
]
