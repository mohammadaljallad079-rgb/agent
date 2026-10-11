"""Document generation tools: write real PDF/DOCX/XLSX/CSV/Markdown/HTML/JSON.

These are the write-side counterparts to :mod:`arabic_agent.tools.builtin.documents`.
They are ordinary :class:`~arabic_agent.tools.base.Tool` instances, so the same
executor, policy, approval, idempotency and audit path applies — creating a file
is a ``WRITE_FILE`` operation and is approval-gated exactly like ``write_file``.

The tools write a real file and report its real byte size; they never return a
description of a file that was not written. A format whose library is absent is
refused with a clear error, and Arabic PDF output uses a resolved Unicode font
so it renders rather than producing missing glyphs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from ...documents.generation import (
    DocumentSpec,
    format_for_suffix,
    generate_document,
    generate_spreadsheet,
    generation_capabilities,
    supported_generation_extensions,
)
from ...local.sandbox import FilesystemSandbox
from ...local.sandbox_provider import SandboxProvider
from ...permissions.base import Permission
from ..base import Tool, ToolContext, ToolResult


def _sandbox(context: ToolContext) -> FilesystemSandbox:
    return SandboxProvider().for_context(context)


def _resolve_write(context: ToolContext, requested: str) -> Path:
    return _sandbox(context).resolve_for_write(requested)


class GenerateDocumentTool(Tool):
    name = "generate_document"
    description = (
        "Create a real document file on disk from a title and content blocks, "
        "in the format implied by the destination extension: .pdf, .docx, .md, "
        ".txt, .html or .json. Arabic text is supported (PDF shapes it with a "
        "Unicode font; DOCX/HTML/etc. are written UTF-8). Returns the real byte "
        "size of the file written. For tabular data use create_spreadsheet. The "
        "file is written inside the workspace and this is an approval-gated write."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Destination path in the workspace; its extension picks the format",
            },
            "title": {"type": "string", "description": "Optional document title"},
            "blocks": {
                "type": "array",
                "description": "Ordered content blocks",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "description": "heading | paragraph | bullet | table | page_break",
                        },
                        "text": {"type": "string"},
                        "level": {"type": "integer"},
                        "items": {"type": "array", "items": {"type": "string"}},
                        "headers": {"type": "array", "items": {"type": "string"}},
                        "rows": {
                            "type": "array",
                            "items": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    permissions = (Permission.WRITE_FILE,)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = _resolve_write(context, arguments["path"])
        spec = DocumentSpec.from_arguments(arguments)
        result = generate_document(path, spec)
        return ToolResult.success(
            f"wrote {result.format} document ({result.bytes_written} bytes) to {arguments['path']}",
            data={
                "path": arguments["path"],
                "format": result.format,
                "bytes_written": result.bytes_written,
                "blocks": result.blocks,
            },
        )


class CreateSpreadsheetTool(Tool):
    name = "create_spreadsheet"
    description = (
        "Create a real spreadsheet (.xlsx or .csv) from headers and rows. Returns "
        "the real byte size written. Arabic and other UTF-8 text is preserved "
        "(CSV is written with a BOM so Excel opens it correctly). This is an "
        "approval-gated write inside the workspace."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Destination .xlsx or .csv path"},
            "sheet": {"type": "string", "description": "Sheet name (xlsx only)"},
            "headers": {"type": "array", "items": {"type": "string"}},
            "rows": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "string"}},
            },
        },
        "required": ["path", "rows"],
        "additionalProperties": False,
    }
    permissions = (Permission.WRITE_FILE,)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = _resolve_write(context, arguments["path"])
        headers = tuple(str(h) for h in (arguments.get("headers") or ()))
        rows = tuple(tuple(str(c) for c in row) for row in (arguments.get("rows") or ()))
        result = generate_spreadsheet(
            path, sheet=str(arguments.get("sheet") or "Sheet1"), headers=headers, rows=rows
        )
        return ToolResult.success(
            f"wrote {result.format} spreadsheet ({result.bytes_written} bytes) "
            f"to {arguments['path']}",
            data={
                "path": arguments["path"],
                "format": result.format,
                "bytes_written": result.bytes_written,
                "rows": result.blocks,
            },
        )


class DocumentGenerationFormatsTool(Tool):
    name = "document_generation_formats"
    description = (
        "Report which document formats this installation can actually generate "
        "(based on which libraries are installed). Read-only."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    permissions = ()

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        caps = generation_capabilities()
        available = sorted(fmt for fmt, ok in caps.items() if ok)
        return ToolResult.success(
            f"generation available for: {', '.join(available)}",
            data={
                "formats": caps,
                "extensions": supported_generation_extensions(),
            },
        )


#: Exported for the container to register.
DOCUMENT_GENERATION_TOOLS: tuple[type[Tool], ...] = (
    GenerateDocumentTool,
    CreateSpreadsheetTool,
    DocumentGenerationFormatsTool,
)


__all__ = [
    "DOCUMENT_GENERATION_TOOLS",
    "CreateSpreadsheetTool",
    "DocumentGenerationFormatsTool",
    "GenerateDocumentTool",
    "format_for_suffix",
]
