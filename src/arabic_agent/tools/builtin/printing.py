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
from ...printing.base import PrintColorMode, PrintDocument, PrintDuplex, PrintOptions
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
        "they are available and accepting jobs, and which is the default. Use "
        "this before printing to choose a printer."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        runtime = self._runtime(context)
        printers = await runtime.list_printers(
            tenant_id=context.tenant_id, user_id=context.user_id, run_id=context.run_id
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
        printer_id = await self._resolve_printer(
            runtime, context, arguments.get("printer_id")
        )
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


#: The printing tools, registered only for a local runtime with printing enabled.
PRINT_TOOLS: tuple[type[Tool], ...] = (
    ListPrintersTool,
    PrintDocumentTool,
)

#: Tool names, so the container can advertise exactly the print tool set.
PRINT_TOOL_NAMES = frozenset(cls.name for cls in PRINT_TOOLS)

__all__ = [
    "PRINT_TOOLS",
    "PRINT_TOOL_NAMES",
    "ListPrintersTool",
    "PrintDocumentTool",
    "print_runtime",
]
