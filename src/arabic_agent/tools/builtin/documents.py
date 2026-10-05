"""Document tools (Phase 6): inspect, read, search and list documents.

These let the agent work with real files (PDF, DOCX, XLSX, PPTX, CSV, HTML,
Markdown, JSON, text, and ZIP/TAR archives) through the existing agent loop. They
are ordinary :class:`~arabic_agent.tools.base.Tool` instances, so the executor,
policy, approval, idempotency and audit path is unchanged.

Design rules baked in here:

* **Metadata first.** ``inspect_document`` returns structure and provenance
  (page/slide/sheet/line locators) without dumping content, so the agent can
  decide what to read.
* **Bounded reads.** ``read_document`` returns at most a token/char budget and
  says what was left out; ``search_document`` ranks sections lexically and
  returns only the best matches, with provenance.
* **No silent truncation.** Every response carries ``truncated`` / ``has_more``
  / ``omitted`` fields so the agent knows more content exists.
* **Secret awareness.** Documents are scanned for likely secrets; a finding is
  reported (kind + locator only, never the value) and marked so the caller can
  keep it local.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from ...documents.loader import inspect_archive, load_document, scan_for_secrets
from ...documents.parsers import ParseLimits, format_for, supported_formats
from ...documents.retrieval import LexicalRetriever
from ...local.sandbox import FilesystemSandbox
from ...local.sandbox_provider import SandboxProvider
from ...permissions.base import Permission
from ..base import Tool, ToolContext, ToolResult

#: Default caps for a document read.
MAX_DOC_TOKENS = 6000
MAX_DOC_CHARS = 24_000


def _sandbox(context: ToolContext) -> FilesystemSandbox:
    return SandboxProvider().for_context(context)


def _resolve_read(context: ToolContext, requested: str) -> Path:
    return _sandbox(context).resolve_for_read(requested)


def _document_limits(context: ToolContext) -> ParseLimits:
    """Parse limits, overridable via tool metadata (kept conservative)."""
    override = context.metadata.get("document_limits")
    if isinstance(override, ParseLimits):
        return override
    return ParseLimits()


class InspectDocumentTool(Tool):
    name = "inspect_document"
    description = (
        "Inspect a document's structure without reading its whole content: "
        "format, metadata (page/slide/sheet counts), section count, a preview of "
        "section locators, and any likely-secret warnings. Use this first, then "
        "read or search the parts you need. Supports text, markdown, json, csv, "
        "html, pdf, docx, xlsx and pptx."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Document path (in an allowed root)"},
            "max_locators": {
                "type": "integer",
                "description": "How many section locators to preview (default 40)",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    permissions = (Permission.FILES_READ,)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = _resolve_read(context, arguments["path"])
        document = load_document(
            path, display_path=arguments["path"], limits=_document_limits(context)
        )
        findings = scan_for_secrets(document)
        max_locators = int(arguments.get("max_locators") or 40)
        locators = [
            {"id": section.id, "kind": section.kind, "locator": section.provenance.locator}
            for section in document.sections[:max_locators]
        ]
        return ToolResult.success(
            f"{document.format} document: {document.section_count} section(s)"
            + ("" if document.has_text else "; no extractable text"),
            data={
                "path": arguments["path"],
                "format": document.format,
                "metadata": document.metadata.model_dump(exclude_none=True),
                "section_count": document.section_count,
                "total_tokens": document.total_tokens(),
                "has_text": document.has_text,
                "truncated": document.truncated,
                "warnings": list(document.warnings),
                # NB: the key name must not itself look like a credential,
                # otherwise the tool boundary's redaction masks the whole list.
                "sensitive_findings": [{"kind": f.kind, "locator": f.locator} for f in findings],
                "locators": locators,
                "supported_formats": list(supported_formats()),
            },
        )


class ReadDocumentTool(Tool):
    name = "read_document"
    description = (
        "Read a bounded portion of a document with provenance. Either read a "
        "section by its id (from inspect_document), or read the first N sections "
        "within a token budget. The result reports what was omitted so you can "
        "request more."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Document path"},
            "section_id": {"type": "string", "description": "Read just this section id"},
            "max_sections": {"type": "integer", "description": "Max sections (default 10)"},
            "max_tokens": {"type": "integer", "description": "Token budget (default 6000)"},
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    permissions = (Permission.FILES_READ,)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        from ...context.budget import estimate_tokens

        path = _resolve_read(context, arguments["path"])
        document = load_document(
            path, display_path=arguments["path"], limits=_document_limits(context)
        )
        if not document.has_text:
            return ToolResult.success(
                "document has no extractable text (it may be scanned images; "
                "OCR is not implemented)",
                data={
                    "path": arguments["path"],
                    "format": document.format,
                    "has_text": False,
                    "warnings": list(document.warnings),
                    "sections": [],
                },
            )
        section_id = arguments.get("section_id")
        if section_id:
            section = document.by_id(section_id)
            if section is None:
                from ...core.errors import NotFoundError

                raise NotFoundError(
                    "unknown section id",
                    details={"section_id": section_id, "path": arguments["path"]},
                )
            return ToolResult.success(
                f"read section {section_id}",
                data={
                    "path": arguments["path"],
                    "format": document.format,
                    "sections": [
                        {
                            "id": section.id,
                            "kind": section.kind,
                            "text": section.text,
                            "provenance": section.provenance.model_dump(exclude_none=True),
                        }
                    ],
                    "omitted": 0,
                    "has_more": False,
                },
            )

        max_sections = int(arguments.get("max_sections") or 10)
        max_tokens = int(arguments.get("max_tokens") or MAX_DOC_TOKENS)
        chosen: list[dict[str, Any]] = []
        tokens_used = 0
        omitted = 0
        for section in document.sections:
            if len(chosen) >= max_sections:
                omitted += 1
                continue
            cost = estimate_tokens(section.text)
            if chosen and tokens_used + cost > max_tokens:
                omitted += 1
                continue
            chosen.append(
                {
                    "id": section.id,
                    "kind": section.kind,
                    "text": section.text,
                    "provenance": section.provenance.model_dump(exclude_none=True),
                }
            )
            tokens_used += cost
        return ToolResult.success(
            f"read {len(chosen)} of {document.section_count} section(s) of {arguments['path']}",
            data={
                "path": arguments["path"],
                "format": document.format,
                "sections": chosen,
                "omitted": omitted,
                "has_more": omitted > 0,
                "truncated": document.truncated,
                "warnings": list(document.warnings),
            },
        )


class SearchDocumentTool(Tool):
    name = "search_document"
    description = (
        "Search a document for the sections most relevant to a query and return "
        "them with provenance. This is the retrieval step: use it instead of "
        "reading a whole document. Works offline with lexical ranking."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Document path"},
            "query": {"type": "string", "description": "What to look for"},
            "limit": {"type": "integer", "description": "Max sections to return (default 5)"},
            "max_tokens": {"type": "integer", "description": "Token budget (default 4000)"},
        },
        "required": ["path", "query"],
        "additionalProperties": False,
    }
    permissions = (Permission.FILES_READ,)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = _resolve_read(context, arguments["path"])
        document = load_document(
            path, display_path=arguments["path"], limits=_document_limits(context)
        )
        result = LexicalRetriever().retrieve(
            document,
            arguments["query"],
            limit=int(arguments.get("limit") or 5),
            max_tokens=int(arguments.get("max_tokens") or 4000),
        )
        return ToolResult.success(
            f"found {len(result.matches)} relevant section(s) in {arguments['path']}",
            data={
                "path": arguments["path"],
                "format": document.format,
                "query": result.query,
                "matches": [
                    {
                        "id": match.section.id,
                        "kind": match.section.kind,
                        "score": round(match.score, 4),
                        "text": match.section.text,
                        "provenance": match.section.provenance.model_dump(exclude_none=True),
                    }
                    for match in result.matches
                ],
                "total_sections": result.total_sections,
                "omitted_sections": len(result.omitted_section_ids),
                "truncated": result.truncated,
                "provenance": list(result.provenance),
            },
        )


class InspectArchiveTool(Tool):
    name = "inspect_archive"
    description = (
        "Safely list the members of a ZIP or TAR archive without extracting or "
        "executing anything. Flags path traversal, absolute paths, symlink "
        "entries and decompression bombs. Use it before deciding to extract."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Archive path"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    permissions = (Permission.FILES_READ,)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = _resolve_read(context, arguments["path"])
        inspection = inspect_archive(path, display_path=arguments["path"])
        return ToolResult.success(
            f"{inspection.format} archive with {len(inspection.entries)} entrie(s)"
            + (f"; {len(inspection.unsafe_entries)} unsafe" if inspection.unsafe_entries else ""),
            data={
                "path": arguments["path"],
                "format": inspection.format,
                "entries": [
                    {
                        "name": e.name,
                        "size": e.size,
                        "is_dir": e.is_dir,
                        "is_symlink": e.is_symlink,
                        "unsafe": e.unsafe,
                        "reason": e.reason,
                    }
                    for e in inspection.entries
                ],
                "total_size": inspection.total_size,
                "unsafe_entries": list(inspection.unsafe_entries),
                "truncated": inspection.truncated,
                "warnings": list(inspection.warnings),
            },
        )


class ListDocumentsTool(Tool):
    name = "list_documents"
    description = (
        "List the documents (by supported format) under a directory, with format "
        "and size, so the agent can discover what is available before reading."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory (default '.')"},
            "max_results": {"type": "integer", "description": "Max files (default 200)"},
        },
        "additionalProperties": False,
    }
    permissions = (Permission.FILES_SEARCH,)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        import os
        from pathlib import Path

        from ..builtin._fileops import SKIP_DIRS

        requested = arguments.get("path") or context.workspace_dir or "."
        sandbox = _sandbox(context)
        root = sandbox.resolve_for_read(requested)
        if not root.is_dir():
            from ...core.errors import NotFoundError

            raise NotFoundError("path is not a directory", details={"path": requested})
        supported = set(supported_formats())
        max_results = int(arguments.get("max_results") or 200)
        found: list[dict[str, Any]] = []
        truncated = False
        for current_dir, dir_names, file_names in os.walk(root):
            dir_names[:] = [d for d in dir_names if d not in SKIP_DIRS]
            for file_name in sorted(file_names):
                candidate = Path(current_dir) / file_name
                fmt = format_for(candidate)
                if fmt == "unknown" or fmt not in supported:
                    continue
                if not sandbox.authorize_read(str(candidate)).allowed:
                    continue
                found.append(
                    {"path": str(candidate), "format": fmt, "size": candidate.stat().st_size}
                )
                if len(found) >= max_results:
                    truncated = True
                    break
            if truncated:
                break
        return ToolResult.success(
            f"found {len(found)} document(s) under {requested}",
            data={
                "path": requested,
                "documents": found,
                "truncated": truncated,
                "supported_formats": list(supported_formats()),
            },
        )


#: The document tools.
DOCUMENT_TOOLS: tuple[type[Tool], ...] = (
    InspectDocumentTool,
    ReadDocumentTool,
    SearchDocumentTool,
    ListDocumentsTool,
    InspectArchiveTool,
)
