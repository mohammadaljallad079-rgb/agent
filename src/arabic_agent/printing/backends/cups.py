"""CUPS printing backend (POSIX).

The POSIX half of the printing seam. It implements the same
:class:`~arabic_agent.printing.base.PrintBackend` contract a future Windows
backend will, so the policy, budget and audit above it are unchanged.

Two layers, deliberately separated:

* :class:`CupsAdapter` is the *only* place that spawns a process. It is a
  narrow, injectable seam: every method returns plain values, so a test can
  substitute a fake adapter to exercise the whole backend without CUPS.
* :class:`CupsBackend` translates those raw values into the shared models and
  reports availability honestly (CUPS tools must be present).

The real adapter uses the CUPS command-line tools with a **shell-free argv
interface** — ``lpstat`` to enumerate, ``lp`` to submit, ``cancel`` to cancel —
so a printer name or option can never inject shell metacharacters. A minimal
environment is passed; API keys and tokens never reach the child. Printer ids
and job ids are validated against a conservative charset and may not begin with
``-``, so a value cannot be mistaken for a flag.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from abc import ABC, abstractmethod
from pathlib import Path

from ...core.enums import PlatformKind
from ...core.errors import ExternalServiceError, NotFoundError, ValidationError
from ..base import (
    PrintBackend,
    PrintColorMode,
    PrintDocument,
    PrintDuplex,
    PrinterCapabilities,
    PrinterInfo,
    PrintJob,
    PrintJobStatus,
    PrintOptions,
    PrintOrientation,
)

logger = logging.getLogger(__name__)

#: Environment variables allowed through to CUPS tools.
_SAFE_ENV_KEYS = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TZ", "TERM"})
#: Conservative charset for spooler names/ids: letters, digits, and ``_ . - @ :``.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@:-]*$")
#: A CUPS job id is ``queue-number``; the queue may itself contain ``-``.
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@:-]*-[0-9]+$")
#: A CUPS page range expression, e.g. ``1-3``, ``1,3,5``.
_PAGE_RANGE_RE = re.compile(r"^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$")
#: A media name, e.g. ``A4``, ``Letter``, ``na_letter_8.5x11in``.
_MEDIA_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
#: How long any single CUPS command may run.
_COMMAND_TIMEOUT_SECONDS = 20.0
#: CUPS job states (RFC 8011 ``job-state``) mapped onto our coarse statuses.
_CUPS_JOB_STATES: dict[int, PrintJobStatus] = {
    3: PrintJobStatus.QUEUED,  # pending
    4: PrintJobStatus.QUEUED,  # pending-held
    5: PrintJobStatus.PRINTING,  # processing
    6: PrintJobStatus.PRINTING,  # processing-stopped
    7: PrintJobStatus.CANCELLED,  # canceled
    8: PrintJobStatus.FAILED,  # aborted
    9: PrintJobStatus.COMPLETED,  # completed
}


def _validate_name(value: str, *, field: str) -> str:
    if not value or not _SAFE_NAME_RE.match(value):
        raise ValidationError(
            f"{field} contains unsupported characters",
            details={"field": field},
        )
    return value


class CupsAdapter(ABC):
    """The narrow seam over the CUPS command-line tools."""

    @abstractmethod
    async def run(
        self,
        argv: list[str],
        *,
        timeout: float = _COMMAND_TIMEOUT_SECONDS,  # noqa: ASYNC109 - a per-command budget
    ) -> tuple[int, str, str]:
        """Run ``argv`` shell-free; return ``(exit_code, stdout, stderr)``."""


class RealCupsAdapter(CupsAdapter):
    """Runs the real CUPS tools with a minimal environment."""

    def _env(self) -> dict[str, str]:
        return {key: os.environ[key] for key in _SAFE_ENV_KEYS if key in os.environ}

    async def run(
        self,
        argv: list[str],
        *,
        timeout: float = _COMMAND_TIMEOUT_SECONDS,  # noqa: ASYNC109 - a per-command budget
    ) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            *argv,
            env=self._env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError as exc:  # pragma: no cover - exercised via fake adapter
            process.kill()
            await process.wait()
            raise ExternalServiceError(f"CUPS command timed out: {argv[0]}") from exc
        return (
            process.returncode or 0,
            stdout_b.decode("utf-8", errors="replace"),
            stderr_b.decode("utf-8", errors="replace"),
        )


class CupsBackend(PrintBackend):
    """A real CUPS printing backend, available when the CUPS tools are present."""

    name = "cups"
    platform = PlatformKind.LINUX

    def __init__(self, *, adapter: CupsAdapter | None = None) -> None:
        self._adapter = adapter or RealCupsAdapter()
        self._lp = shutil.which("lp")
        self._lpstat = shutil.which("lpstat")
        self._cancel = shutil.which("cancel")
        # ``lpoptions`` is optional: it is only needed to read a printer's
        # advertised capabilities. Its absence degrades capabilities to
        # ``None`` (honestly unknown) rather than making printing unavailable.
        self._lpoptions = shutil.which("lpoptions")
        if self._lp and self._lpstat:
            self.available = True
            self.unavailable_reason = ""
        else:
            missing = [
                tool for tool, path in (("lp", self._lp), ("lpstat", self._lpstat)) if not path
            ]
            self.unavailable_reason = (
                f"the CUPS tools are not installed (missing: {', '.join(missing)})"
            )

    async def list_printers(self) -> list[PrinterInfo]:
        self.require_available()
        assert self._lpstat is not None
        code, out, err = await self._adapter.run([self._lpstat, "-p"])
        if code != 0:
            raise ExternalServiceError(
                "could not list printers", details={"detail": err.strip()[:400]}
            )
        default_id = await self._default_printer()
        printers: list[PrinterInfo] = []
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("printer "):
                continue
            rest = line[len("printer ") :]
            name = rest.split(" ", 1)[0]
            lowered = rest.lower()
            accepting = "disabled" not in lowered and "stopped" not in lowered
            available = accepting and "not available" not in lowered
            printers.append(
                PrinterInfo(
                    id=name,
                    name=name,
                    available=available,
                    accepting_jobs=accepting,
                    is_default=(name == default_id),
                    backend=self.name,
                    detail=rest[len(name) :].strip(),
                )
            )
        return printers

    async def printer_capabilities(self, printer_id: str) -> PrinterCapabilities | None:
        if not self.available or not self._lpoptions:
            return None
        try:
            printer = _validate_name(printer_id, field="printer_id")
        except ValidationError:
            return None
        code, out, _ = await self._adapter.run([self._lpoptions, "-p", printer, "-l"])
        if code != 0 or not out.strip():
            return None
        return _parse_lpoptions(out)

    async def _default_printer(self) -> str | None:
        assert self._lpstat is not None
        code, out, _ = await self._adapter.run([self._lpstat, "-d"])
        if code != 0:
            return None
        # "system default destination: HP_LaserJet"
        for line in out.splitlines():
            if ":" in line:
                return line.split(":", 1)[1].strip() or None
        return None

    def _option_flags(self, options: PrintOptions) -> list[str]:
        flags: list[str] = []
        if options.copies and options.copies != 1:
            flags += ["-n", str(options.copies)]
        if options.color is PrintColorMode.MONOCHROME:
            flags += ["-o", "ColorModel=Gray"]
        if options.duplex is PrintDuplex.LONG_EDGE:
            flags += ["-o", "sides=two-sided-long-edge"]
        elif options.duplex is PrintDuplex.SHORT_EDGE:
            flags += ["-o", "sides=two-sided-short-edge"]
        if options.orientation is PrintOrientation.LANDSCAPE:
            flags += ["-o", "orientation-requested=4"]
        if options.media:
            if not _MEDIA_RE.match(options.media):
                raise ValidationError("media contains unsupported characters")
            flags += ["-o", f"media={options.media}"]
        if options.page_range:
            if not _PAGE_RANGE_RE.match(options.page_range):
                raise ValidationError("page_range is not a valid page range")
            flags += ["-o", f"page-ranges={options.page_range}"]
        return flags

    async def submit(
        self, *, document: PrintDocument, printer_id: str, options: PrintOptions
    ) -> PrintJob:
        self.require_available()
        assert self._lp is not None
        printer = _validate_name(printer_id, field="printer_id")
        path = Path(document.path)
        # A local stat before handing the path to the spooler; cheap and, unlike
        # an async wrapper, cannot hide a permission error.
        if not await asyncio.to_thread(path.is_file):
            raise NotFoundError("document to print was not found", details={"path": document.path})
        title = document.title or path.name
        argv = [
            self._lp,
            "-d",
            printer,
            *self._option_flags(options),
            "-t",
            title,
            "--",
            str(path),
        ]
        code, out, err = await self._adapter.run(argv)
        if code != 0:
            raise ExternalServiceError(
                "the print job was rejected by the spooler",
                details={"detail": err.strip()[:400]},
            )
        job_id = _parse_job_id(out) or _parse_job_id(err)
        if not job_id:
            # The spooler accepted it but did not name the job; be honest.
            job_id = f"{printer}-unknown"
        return PrintJob(
            id=job_id,
            printer_id=printer,
            title=title,
            status=PrintJobStatus.QUEUED,
            detail=out.strip()[:200],
            backend=self.name,
            submitted_at=time.time(),
        )

    async def job_status(self, job_id: str, *, printer_id: str = "") -> PrintJobStatus:
        if not self.available:
            return PrintJobStatus.UNKNOWN
        assert self._lpstat is not None
        try:
            safe = _validate_name(job_id, field="job_id")
        except ValidationError:
            return PrintJobStatus.UNKNOWN
        # ``lpstat -l -o`` prints one block per queued job with a
        # ``job-state`` line whose numeric value maps onto our statuses.
        code, out, _ = await self._adapter.run([self._lpstat, "-l", "-o"])
        if code != 0:
            return PrintJobStatus.UNKNOWN
        return _parse_job_state(out, safe)

    async def cancel_job(self, job_id: str, *, printer_id: str = "") -> bool:
        if not self._cancel:
            return False
        try:
            safe = _validate_name(job_id, field="job_id")
        except ValidationError:
            return False
        code, _, _ = await self._adapter.run([self._cancel, safe])
        return code == 0


def _parse_job_id(text: str) -> str | None:
    """Extract the job id from ``lp`` output: ``request id is NAME-3 (...)``."""
    match = re.search(r"request id is (\S+)", text)
    return match.group(1) if match else None


def _parse_job_state(out: str, job_id: str) -> PrintJobStatus:
    """Parse ``lpstat -l -o`` output for one job's ``job-state``.

    The output is a sequence of ``job_id  ...`` header lines followed by
    indented attribute lines; we locate the requested job and read its
    ``job-state: <n>``. A job that is no longer listed is reported ``UNKNOWN``
    (CUPS does not retain completed-job history here), never assumed done.
    """
    current: str | None = None
    for line in out.splitlines():
        if line and not line[0].isspace():
            current = line.split()[0]
            continue
        if current != job_id:
            continue
        match = re.search(r"job-state:\s*(\d+)", line)
        if match:
            return _CUPS_JOB_STATES.get(int(match.group(1)), PrintJobStatus.UNKNOWN)
    return PrintJobStatus.UNKNOWN


def _parse_lpoptions(out: str) -> PrinterCapabilities:
    """Parse ``lpoptions -l`` output into capabilities.

    Each line is ``Key/Description: *Default Value1 Value2 ...``. We translate
    the CUPS option vocabulary (PageSize/ColorModel/Duplex/Orientation) into the
    provider-neutral capability model, marking nothing as supported when the
    key is absent.
    """
    media: list[str] = []
    color_modes: list[PrintColorMode] = []
    duplex_modes: list[PrintDuplex] = []
    orientations: list[PrintOrientation] = []
    seen: set[str] = set()
    for line in out.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        key_part, _, values_part = line.partition(":")
        key = key_part.split("/", 1)[0].strip()
        values = [v.lstrip("*") for v in values_part.split() if v and v != "*"]
        if key == "PageSize":
            media = values
            seen.add(key)
        elif key == "ColorModel":
            color_modes = [
                PrintColorMode.MONOCHROME
                if v.lower() in ("gray", "monochrome")
                else PrintColorMode.COLOR
                for v in values
            ]
            seen.add(key)
        elif key == "Duplex":
            mapping = {
                "none": PrintDuplex.ONE_SIDED,
                "duplexnotumble": PrintDuplex.LONG_EDGE,
                "duplextumble": PrintDuplex.SHORT_EDGE,
            }
            duplex_modes = [mapping[v.lower()] for v in values if v.lower() in mapping]
            seen.add(key)
        elif key == "Orientation":
            mapping_o = {
                "3": PrintOrientation.PORTRAIT,
                "portrait": PrintOrientation.PORTRAIT,
                "4": PrintOrientation.LANDSCAPE,
                "landscape": PrintOrientation.LANDSCAPE,
            }
            orientations = [mapping_o[v.lower()] for v in values if v.lower() in mapping_o]
            seen.add(key)
    if not seen:
        return PrinterCapabilities(detail="the printer reported no option list")
    return PrinterCapabilities(
        media=tuple(media) if "PageSize" in seen else None,
        color_modes=tuple(color_modes) if "ColorModel" in seen else None,
        duplex_modes=tuple(duplex_modes) if "Duplex" in seen else None,
        orientations=tuple(orientations) if "Orientation" in seen else None,
        supports_page_range=True,
    )


__all__ = ["CupsAdapter", "CupsBackend", "RealCupsAdapter"]
