"""Printing tools: send a document to a real printer.

These are ordinary :class:`~arabic_agent.tools.base.Tool` instances, so they run
through the existing executor: the permission/policy layer decides allow / deny /
requires-approval, approvals pause the run, and every call is audited. The tools
themselves contain no OS logic — they translate model arguments into a
:class:`~arabic_agent.printing.runtime.PrintRuntime` call, which enforces policy,
the per-run budget, the data boundary and audit.

The document path is resolved through the *filesystem sandbox* first, so a print
request can only ever read a file the agent is already allowed to read — it can
never be used to exfiltrate an arbitrary file to a printer. They are registered
only for a local runtime and only when printing is enabled, so on a cloud build
the model cannot even name them.
"""

from __future__ import annotations

from typing import Any, ClassVar

from ...core.errors import ConfigurationError, NotFoundError, ValidationError
from ...permissions.base import Permission
from ...printing.base import (
    PrintColorMode,
    PrintDocument,
    PrintDuplex,
    PrintJobStatus,
    PrintOptions,
    PrintOrientation,
)
from ...printing.runtime import PrintRuntime
from ..base import Tool, ToolContext, ToolResult
from .local import sandbox_for

#: Extension → MIME type, used to gate what may be printed. An unknown extension
#: is refused rather than assumed printable.
_CONTENT_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def print_runtime(context: ToolContext) -> PrintRuntime:
    """Fetch the print runtime from the tool context, or fail honestly."""
    runtime = context.metadata.get("print_runtime")
    if not isinstance(runtime, PrintRuntime):
        raise ConfigurationError("printing is not available in this runtime")
    return runtime


def _content_type_for(path: str) -> str:
    import os

    ext = os.path.splitext(path)[1].lower()
    content_type = _CONTENT_TYPES.get(ext)
    if content_type is None:
        raise ValidationError(
            "this file type cannot be printed",
            details={"extension": ext or "(none)"},
        )
    return content_type


class _PrintTool(Tool):
    """Shared base: declares that it needs the print runtime."""

    permissions: tuple[Permission, ...] = (Permission.PRINT,)

    @staticmethod
    def _runtime(context: ToolContext) -> PrintRuntime:
        return print_runtime(context)


class ListPrintersTool(_PrintTool):
    name = "list_printers"
    description = (
        "List the printers available on this machine, with their ids, whether "
        "they are available and accepting jobs, and which is the default. Set "
        "include_capabilities to also report each printer's advertised paper "
        "sizes, colour/duplex modes and orientation support. Use this before "
        "printing to choose a printer."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "include_capabilities": {
                "type": "boolean",
                "description": "Also query each printer's advertised capabilities",
            }
        },
        "additionalProperties": False,
    }

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        runtime = self._runtime(context)
        with_caps = bool(arguments.get("include_capabilities"))
        printers = await runtime.list_printers(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            run_id=context.run_id,
            with_capabilities=with_caps,
        )
        data = {
            "count": len(printers),
            "printers": [
                {
                    "id": p.id,
                    "name": p.display_name,
                    "available": p.available,
                    "accepting_jobs": p.accepting_jobs,
                    "default": p.is_default,
                    "detail": p.detail,
                    "capabilities": p.capabilities.model_dump() if p.capabilities else None,
                }
                for p in printers
            ],
        }
        if not printers:
            return ToolResult.success("no printers are configured on this machine", data=data)
        return ToolResult.success(f"found {len(printers)} printer(s)", data=data)


class PrintDocumentTool(_PrintTool):
    name = "print_document"
    description = (
        "Print a file from an allowed local directory. The path is resolved "
        "inside the sandbox; the file must already exist and be a printable "
        "type (PDF, DOCX, XLSX, PPTX, CSV, text, image). If no printer is "
        "given, the default printer is used. Printing is a physical side "
        "effect and always requires approval."
    )
    side_effecting = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to print"},
            "printer_id": {
                "type": "string",
                "description": "Printer id from list_printers (default printer if omitted)",
            },
            "title": {"type": "string", "description": "Job title (defaults to the file name)"},
            "copies": {"type": "integer", "description": "Number of copies (1-99)"},
            "color": {
                "type": "string",
                "enum": [mode.value for mode in PrintColorMode],
                "description": "Colour mode",
            },
            "duplex": {
                "type": "string",
                "enum": [mode.value for mode in PrintDuplex],
                "description": "Two-sided printing mode",
            },
            "orientation": {
                "type": "string",
                "enum": [mode.value for mode in PrintOrientation],
                "description": "Page orientation",
            },
            "media": {"type": "string", "description": "Paper size, e.g. A4 or Letter"},
            "page_range": {"type": "string", "description": "Page range, e.g. 1-3"},
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def _options(self, arguments: dict[str, Any]) -> PrintOptions:
        try:
            return PrintOptions(
                copies=int(arguments.get("copies") or 1),
                color=PrintColorMode(arguments.get("color") or PrintColorMode.COLOR.value),
                duplex=PrintDuplex(arguments.get("duplex") or PrintDuplex.ONE_SIDED.value),
                orientation=PrintOrientation(
                    arguments.get("orientation") or PrintOrientation.PORTRAIT.value
                ),
                media=arguments.get("media") or None,
                page_range=arguments.get("page_range") or None,
            )
        except ValueError as exc:
            raise ValidationError(f"invalid print option: {exc}") from exc

    async def _resolve_printer(
        self, runtime: PrintRuntime, context: ToolContext, requested: str | None
    ) -> str:
        if requested:
            return requested
        printers = await runtime.list_printers(
            tenant_id=context.tenant_id, user_id=context.user_id, run_id=context.run_id
        )
        if not printers:
            raise NotFoundError("no printers are configured on this machine")
        default = next((p for p in printers if p.is_default), None)
        if default is not None:
            return default.id
        accepting = next((p for p in printers if p.accepting_jobs and p.available), None)
        if accepting is not None:
            return accepting.id
        return printers[0].id

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        runtime = self._runtime(context)
        requested = arguments["path"]
        if not isinstance(requested, str) or not requested.strip():
            raise ValidationError("path must be a non-empty string")
        resolved = sandbox_for(context).resolve_for_read(requested)
        content_type = _content_type_for(str(resolved))
        printer_id = await self._resolve_printer(runtime, context, arguments.get("printer_id"))
        options = self._options(arguments)
        document = PrintDocument(
            path=str(resolved),
            title=arguments.get("title") or "",
            content_type=content_type,
        )
        job = await runtime.submit(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            run_id=context.run_id,
            document=document,
            printer_id=printer_id,
            options=options,
            resolved_path=str(resolved),
        )
        data = {
            "job_id": job.id,
            "printer_id": job.printer_id,
            "title": job.title,
            "status": job.status.value,
            "copies": options.copies,
            "backend": job.backend,
        }
        return ToolResult.success(
            f"sent {job.title} to printer {job.printer_id} (job {job.id})", data=data
        )


class ListPrintJobsTool(_PrintTool):
    name = "list_print_jobs"
    description = (
        "List print jobs this machine has submitted, most recent first, with "
        "their ids, printers, titles, sizes and observed status. Use this to "
        "review or reconcile past prints, or to find a job id to check or cancel."
    )
    # Read-only discovery of local job metadata; no permission is required (the
    # records are tenant-isolated and contain no document content).
    permissions: tuple[Permission, ...] = ()
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "Only jobs from this run (defaults to recent jobs)",
            },
            "limit": {"type": "integer", "description": "Maximum jobs to return (1-200)"},
        },
        "additionalProperties": False,
    }

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        runtime = self._runtime(context)
        limit = int(arguments.get("limit") or 50)
        limit = max(1, min(limit, 200))
        records = await runtime.list_jobs(
            tenant_id=context.tenant_id,
            run_id=arguments.get("run_id") or None,
            limit=limit,
        )
        data = {
            "count": len(records),
            "jobs": [
                {
                    "job_id": r.id,
                    "printer_id": r.printer_id,
                    "title": r.title,
                    "status": r.status.value,
                    "observed": r.observed,
                    "size_bytes": r.size_bytes,
                    "run_id": r.run_id,
                    "created_at": r.created_at.isoformat(),
                }
                for r in records
            ],
        }
        if not records:
            return ToolResult.success("no print jobs have been submitted", data=data)
        return ToolResult.success(f"{len(records)} print job(s)", data=data)


class PrintJobStatusTool(_PrintTool):
    name = "print_job_status"
    description = (
        "Check the current status of a previously submitted print job by its "
        "job id. The status is read back from the printer spooler; when it "
        "cannot be observed the status is reported as 'unknown' rather than "
        "assumed complete."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "Job id from print_document"},
            "printer_id": {
                "type": "string",
                "description": "Printer id (optional; taken from the job record)",
            },
        },
        "required": ["job_id"],
        "additionalProperties": False,
    }

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        runtime = self._runtime(context)
        job_id = str(arguments["job_id"])
        try:
            record = await runtime.job_status(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                run_id=context.run_id,
                job_id=job_id,
                printer_id=str(arguments.get("printer_id") or ""),
            )
        except NotFoundError:
            return ToolResult.failure(
                f"no print job {job_id} is recorded for this tenant",
                data={"job_id": job_id, "status": PrintJobStatus.UNKNOWN.value},
            )
        data = {
            "job_id": record.id,
            "printer_id": record.printer_id,
            "title": record.title,
            "status": record.status.value,
            "observed": record.observed,
            "detail": record.detail,
            "error": record.error,
        }
        return ToolResult.success(f"print job {record.id} is {record.status.value}", data=data)


class CancelPrintJobTool(_PrintTool):
    name = "cancel_print_job"
    description = (
        "Cancel a previously submitted print job by its job id. Cancelling is "
        "consequential and always requires approval. Only jobs recorded for "
        "this tenant can be cancelled."
    )
    side_effecting = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "Job id to cancel"},
            "printer_id": {
                "type": "string",
                "description": "Printer id (optional; taken from the job record)",
            },
        },
        "required": ["job_id"],
        "additionalProperties": False,
    }

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        runtime = self._runtime(context)
        job_id = str(arguments["job_id"])
        cancelled = await runtime.cancel(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            run_id=context.run_id,
            job_id=job_id,
            printer_id=str(arguments.get("printer_id") or ""),
        )
        if not cancelled:
            return ToolResult.failure(
                f"the spooler refused to cancel print job {job_id}",
                data={"job_id": job_id, "cancelled": False},
            )
        return ToolResult.success(
            f"cancelled print job {job_id}", data={"job_id": job_id, "cancelled": True}
        )


#: The printing tools, registered only for a local runtime with printing enabled.
PRINT_TOOLS: tuple[type[Tool], ...] = (
    ListPrintersTool,
    PrintDocumentTool,
    PrintJobStatusTool,
    ListPrintJobsTool,
    CancelPrintJobTool,
)

#: Tool names, so the container can advertise exactly the print tool set.
PRINT_TOOL_NAMES = frozenset(cls.name for cls in PRINT_TOOLS)

__all__ = [
    "PRINT_TOOLS",
    "PRINT_TOOL_NAMES",
    "CancelPrintJobTool",
    "ListPrintJobsTool",
    "ListPrintersTool",
    "PrintDocumentTool",
    "PrintJobStatusTool",
    "print_runtime",
]
