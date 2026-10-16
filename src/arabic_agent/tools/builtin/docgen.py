"""Document generation tools: write real PDF/DOCX/PPTX/XLSX/CSV/Markdown/HTML/JSON.

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

from ...core.errors import ValidationError
from ...documents.editing import (
    edit_document_in_place,
    edit_xlsx_cell,
    editable_extensions,
    editing_capabilities,
    export_document,
    export_extensions,
    preview_edit,
)
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


def _resolve_read(context: ToolContext, requested: str) -> Path:
    return _sandbox(context).resolve_for_read(requested)


class GenerateDocumentTool(Tool):
    name = "generate_document"
    description = (
        "Create a real document file on disk from a title and content blocks, "
        "in the format implied by the destination extension: .pdf, .docx, .pptx, "
        ".md, .txt, .html or .json. Arabic text is supported (PDF shapes it with "
        "a Unicode font/shaper; PPTX marks paragraphs right-to-left for the "
        "renderer; DOCX/HTML/etc. are written UTF-8). Returns "
        "the real byte size of the file written. For tabular data use "
        "create_spreadsheet; for slides prefer create_presentation. The file is "
        "written inside the workspace and this is an approval-gated write."
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


class CreatePresentationTool(Tool):
    name = "create_presentation"
    description = (
        "Create a real PowerPoint presentation (.pptx) from a title and content "
        "blocks. Each heading starts a new slide; paragraphs, bullets and tables "
        "become the current slide's body. Arabic is supported: paragraphs are "
        "marked right-to-left so PowerPoint renders Arabic correctly, and the "
        "logical text is preserved for copy/paste and search. Returns "
        "the real byte size of the file written. The file is written inside the "
        "workspace and this is an approval-gated write."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Destination .pptx path in the workspace",
            },
            "title": {"type": "string", "description": "Optional deck title (first slide)"},
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
        if format_for_suffix(path.suffix) != "pptx":
            raise ValidationError(f"create_presentation writes .pptx only, not {path.suffix!r}")
        spec = DocumentSpec.from_arguments(arguments)
        result = generate_document(path, spec)
        return ToolResult.success(
            f"wrote {result.format} presentation ({result.bytes_written} bytes) "
            f"to {arguments['path']}",
            data={
                "path": arguments["path"],
                "format": result.format,
                "bytes_written": result.bytes_written,
                "blocks": result.blocks,
            },
        )


class EditDocumentTool(Tool):
    name = "edit_document"
    description = (
        "Modify an EXISTING document in place. Text formats (.md, .txt, .html, "
        ".json, .csv) and .docx use exact-text replacement: pass 'find' and "
        "'replace' (optionally 'replace_all', or 'expected_replacements' to fail "
        "safely on an unexpected match count). A spreadsheet (.xlsx) is edited by "
        "cell: pass 'cell' (e.g. 'B4'), 'replace' as the new value and optionally "
        "'sheet'. PDF and PPTX cannot be edited in place and are refused — "
        "regenerate them from a source instead. The write is atomic (a temporary "
        "file is replaced into place), stays inside the workspace, and is an "
        "approval-gated write."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Existing document path in the workspace"},
            "find": {"type": "string", "description": "Exact text to find (text/docx formats)"},
            "replace": {"type": "string", "description": "Replacement text, or the new cell value"},
            "replace_all": {
                "type": "boolean",
                "description": "Replace every occurrence (default: require a unique match)",
            },
            "expected_replacements": {
                "type": "integer",
                "description": "Fail unless exactly this many occurrences are found",
            },
            "cell": {"type": "string", "description": "Target cell for .xlsx (e.g. 'B4')"},
            "sheet": {"type": "string", "description": "Sheet name for .xlsx (default: active)"},
        },
        "required": ["path", "replace"],
        "additionalProperties": False,
    }
    permissions = (Permission.WRITE_FILE,)

    async def preview(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any] | None:
        path = _resolve_write(context, arguments["path"])
        return preview_edit(
            path,
            find=str(arguments.get("find") or ""),
            replace=str(arguments.get("replace") or ""),
            replace_all=bool(arguments.get("replace_all", False)),
            cell=arguments.get("cell"),
            sheet=arguments.get("sheet"),
        )

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = _resolve_write(context, arguments["path"])
        if path.suffix.lower() == ".xlsx":
            result = edit_xlsx_cell(
                path,
                sheet=arguments.get("sheet"),
                cell=str(arguments.get("cell") or ""),
                value=str(arguments["replace"]),
            )
        else:
            find = arguments.get("find")
            if find is None:
                raise ValidationError(
                    "editing a text/document file requires 'find' (the exact text to replace)"
                )
            result = edit_document_in_place(
                path,
                find=str(find),
                replace=str(arguments["replace"]),
                replace_all=bool(arguments.get("replace_all", False)),
                expected_replacements=arguments.get("expected_replacements"),
            )
        return ToolResult.success(
            f"edited {result.format} document ({result.replacements} replacement(s), "
            f"{result.bytes_written} bytes) at {arguments['path']}",
            data={"path": arguments["path"], **result.as_dict()},
        )


class ExportDocumentTool(Tool):
    name = "export_document"
    description = (
        "Convert an existing document to another supported format. The source is "
        "read with the document parsers and written with the real writers, so the "
        "output is a genuine file of the target format (Arabic-aware). Supported "
        "targets: .pdf, .docx, .xlsx, .md, .txt, .html, .json, .csv. An image-only "
        "PDF (no extractable text) is refused rather than producing an empty file. "
        "The write is atomic, stays inside the workspace, and is an approval-gated "
        "write."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Existing document to read"},
            "destination": {
                "type": "string",
                "description": "Destination path; its extension picks the target format",
            },
            "title": {"type": "string", "description": "Optional title override for the output"},
        },
        "required": ["source", "destination"],
        "additionalProperties": False,
    }
    permissions = (Permission.WRITE_FILE,)

    async def preview(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any] | None:
        source = _resolve_read(context, arguments["source"])
        destination = _resolve_write(context, arguments["destination"])
        target_format = format_for_suffix(destination.suffix) or destination.suffix.lstrip(".")
        preview: dict[str, Any] = {
            "operation": "export_document",
            "source": str(source),
            "destination": str(destination),
            "target_format": target_format,
            "will_change": True,
        }
        if not source.exists():
            preview["will_change"] = False
            preview["note"] = "source does not exist"
        elif destination.exists():
            preview["overwrites_existing"] = True
        return preview

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        source = _resolve_read(context, arguments["source"])
        destination = _resolve_write(context, arguments["destination"])
        result = export_document(source, destination, title=arguments.get("title"))
        return ToolResult.success(
            f"exported {result.source_format} to {result.target_format} "
            f"({result.bytes_written} bytes) at {arguments['destination']}",
            data={"source": arguments["source"], **result.as_dict()},
        )


class DocumentEditingFormatsTool(Tool):
    name = "document_editing_formats"
    description = (
        "Report which document formats this installation can edit in place and "
        "which it can export to (based on which libraries are installed). "
        "Read-only."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    permissions = ()

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        caps = editing_capabilities()
        editable = sorted(fmt for fmt, c in caps.items() if c["editable"])
        exportable = sorted(fmt for fmt, c in caps.items() if c["exportable"])
        return ToolResult.success(
            f"editable: {', '.join(editable)}; exportable: {', '.join(exportable)}",
            data={
                "formats": caps,
                "editable_extensions": editable_extensions(),
                "export_extensions": export_extensions(),
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
    CreatePresentationTool,
    DocumentGenerationFormatsTool,
    EditDocumentTool,
    ExportDocumentTool,
    DocumentEditingFormatsTool,
)


__all__ = [
    "DOCUMENT_GENERATION_TOOLS",
    "CreatePresentationTool",
    "CreateSpreadsheetTool",
    "DocumentEditingFormatsTool",
    "DocumentGenerationFormatsTool",
    "EditDocumentTool",
    "ExportDocumentTool",
    "GenerateDocumentTool",
    "format_for_suffix",
]
