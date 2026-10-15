"""Real document editing and export.

This is the *modify* counterpart to :mod:`arabic_agent.documents.generation`
(create) and :mod:`arabic_agent.documents.parsers` (read). It edits an existing
document in place and exports a parsed document to another supported format,
writing through a temporary file and an atomic replacement so a crash or a
failed conversion never leaves a half-written document behind.

Design rules (matching the rest of the ``documents`` package):

* **Honest capability reporting.** :func:`editing_capabilities` reports, per
  format, whether it can be edited *and* whether it can be exported in *this*
  install. An edit or conversion whose library is absent raises a clear error
  rather than silently producing a broken file.
* **Refuse, do not fake.** A conversion that cannot preserve the content (an
  image-only PDF, a format that has no writer) is refused with an actionable
  message. Nothing is written that was not requested.
* **Arabic-first.** Text written back is UTF-8; a converted document goes
  through the same Arabic-aware writer as generation, so a converted PDF shapes
  Arabic and a converted DOCX/HTML is RTL.
* **Bounded.** Every read and every written document is capped (``MAX_TOTAL_CHARS``
  from generation, the parser limits from ingestion) so a runaway edit cannot
  exhaust memory.
* **Atomic.** Writes go to a sibling temporary file that is ``os.replace``-d into
  place, so the destination is either the old file or the new one — never a
  partial one.

Nothing here resolves a path or checks a permission: the *tool* wrapper does
that through the workspace sandbox and the policy engine, and only a resolved
path inside the workspace is ever handed to these functions.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.errors import ConfigurationError, NotFoundError, ValidationError
from .generation import (
    Block,
    DocumentSpec,
    format_for_suffix,
    generate_document,
    generate_spreadsheet,
    generation_capabilities,
    sanitize_sheet_name,
)
from .loader import load_document
from .models import Document

#: Hard cap on the bytes read from a source when re-parsing text for export.
_MAX_SOURCE_BYTES = 8 * 1024 * 1024

#: Formats that can be edited in place. A read-only format (PDF) is absent: PDF
#: has no reliable in-place text edit, so ``edit_document`` refuses it and the
#: caller is told to regenerate the PDF from a source document instead.
_EDITABLE_FORMATS: dict[str, str] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".html": "html",
    ".htm": "html",
    ".json": "json",
    ".csv": "csv",
    ".docx": "docx",
    ".xlsx": "xlsx",
}

#: Format → the extensions a document can be *exported* to. Only formats with a
#: real writer are listed; a target whose library is absent is refused at call
#: time by :func:`generation_capabilities`.
_EXPORT_TARGETS: dict[str, tuple[str, ...]] = {
    "pdf": (".pdf",),
    "docx": (".docx",),
    "markdown": (".md", ".markdown"),
    "text": (".txt",),
    "html": (".html", ".htm"),
    "json": (".json",),
    "csv": (".csv",),
    "xlsx": (".xlsx",),
}

#: A heading line in a Markdown document: ``# Title``.
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
#: A bullet line in a Markdown document: ``- item`` / ``* item``.
_MD_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
#: A Markdown table separator row: ``| --- | :--: |``.
_MD_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def editing_capabilities() -> dict[str, dict[str, bool]]:
    """Per-format edit/export capability for *this* install.

    ``editable`` is whether ``edit_document`` can modify an existing file of
    that format; ``exportable`` is whether that format can be produced as an
    export target (a superset that includes read-only sources such as PDF).
    """
    gen = generation_capabilities()
    return {
        "pdf": {"editable": False, "exportable": gen["pdf"]},
        "docx": {"editable": gen["docx"], "exportable": gen["docx"]},
        "pptx": {"editable": False, "exportable": gen["pptx"]},
        "xlsx": {"editable": gen["xlsx"], "exportable": gen["xlsx"]},
        "csv": {"editable": True, "exportable": True},
        "markdown": {"editable": True, "exportable": True},
        "text": {"editable": True, "exportable": True},
        "html": {"editable": True, "exportable": True},
        "json": {"editable": True, "exportable": True},
    }


def is_editable_extension(suffix: str) -> bool:
    return suffix.lower() in _EDITABLE_FORMATS


def editable_extensions() -> list[str]:
    return sorted(_EDITABLE_FORMATS)


def export_extensions() -> list[str]:
    return sorted(ext for exts in _EXPORT_TARGETS.values() for ext in exts)


def _atomic_write_bytes(path: Path, data: bytes) -> int:
    """Write ``data`` to ``path`` atomically, returning the real byte size.

    A sibling temporary file in the same directory is written, flushed and
    ``os.replace``-d into place. ``os.replace`` is atomic on the same
    filesystem, so a reader sees either the old file or the complete new one —
    never a partially written document. The temporary file is removed if the
    write fails.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.aap-tmp")
    try:
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path.stat().st_size


def _atomic_replace_with(writer: Any, path: Path) -> int:
    """Run a generator ``writer(tmp_path)`` then atomically move it into place.

    The generators (PDF/DOCX/XLSX/…) take a destination path and write the file
    themselves. They are pointed at a sibling temporary file, and only a
    successful write is promoted to ``path``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.aap-tmp{path.suffix}")
    try:
        writer(tmp)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path.stat().st_size


# --- Editing ---------------------------------------------------------------


@dataclass(frozen=True)
class EditResult:
    """The outcome of a real in-place document edit."""

    path: str
    format: str
    bytes_written: int
    replacements: int
    atomic: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "format": self.format,
            "bytes_written": self.bytes_written,
            "replacements": self.replacements,
            "atomic": self.atomic,
        }


def _read_text_bounded(path: Path, *, max_chars: int) -> str:
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8", errors="replace")
    if len(text) > max_chars:
        raise ValidationError(
            f"document is too large to edit ({len(text)} chars; limit {max_chars})",
            details={"path": str(path), "limit": max_chars},
        )
    return text


def _count_and_check(text: str, old: str, *, replace_all: bool, expected: int | None) -> int:
    if not old:
        raise ValidationError("'find' text must not be empty")
    occurrences = text.count(old)
    if occurrences == 0:
        raise NotFoundError(
            "the 'find' text was not found in the document",
            details={"find": old[:80]},
        )
    if expected is not None and occurrences != expected:
        raise ValidationError(
            "replacement count did not match expectation",
            details={"found": occurrences, "expected": expected},
        )
    if occurrences > 1 and not replace_all:
        raise ValidationError(
            "'find' appears more than once; pass replace_all=true or a larger unique block",
            details={"found": occurrences},
        )
    return occurrences


def edit_text_document(
    path: Path,
    *,
    find: str,
    replace: str,
    replace_all: bool = False,
    expected_replacements: int | None = None,
    max_chars: int = 2_000_000,
) -> EditResult:
    """Replace exact text in a text-based document, atomically.

    The file is read (bounded), the occurrence count is checked against
    ``expected_replacements`` when supplied, and the result is written through a
    temporary file and an atomic replace. Encoding is preserved (UTF-8 with or
    without a BOM) so Arabic content round-trips.
    """
    fmt = format_for_suffix(path.suffix) or "text"
    if not is_editable_extension(path.suffix):
        raise ValidationError(
            f"format {path.suffix!r} cannot be edited in place",
            details={"supported": editable_extensions()},
        )
    if not path.exists():
        raise NotFoundError("document does not exist", details={"path": str(path)})
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = _read_text_bounded(path, max_chars=max_chars)
    replacements = _count_and_check(
        text, find, replace_all=replace_all, expected=expected_replacements
    )
    updated = text.replace(find, replace) if replace_all else text.replace(find, replace, 1)
    encoding = "utf-8-sig" if bom else "utf-8"
    size = _atomic_write_bytes(path, updated.encode(encoding))
    return EditResult(
        path=str(path),
        format=fmt,
        bytes_written=size,
        replacements=replacements if replace_all else 1,
    )


def _iter_docx_paragraphs(document: Any) -> list[Any]:
    """Every paragraph in the document body *and* inside tables, in order."""
    paragraphs = list(document.paragraphs)
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                paragraphs.extend(cell.paragraphs)
    return paragraphs


def edit_docx_document(
    path: Path,
    *,
    find: str,
    replace: str,
    replace_all: bool = False,
    expected_replacements: int | None = None,
) -> EditResult:
    """Replace exact text across a DOCX's paragraphs and table cells.

    A match must live inside a single run; python-docx models a paragraph as a
    list of runs and splitting text across runs is common, so a paragraph whose
    full text matches is rewritten into its first run (the rest are cleared).
    This preserves the paragraph's style and the run's formatting. The result is
    written atomically.
    """
    try:
        import docx
    except ImportError as exc:  # pragma: no cover - dependency presence varies
        raise ConfigurationError(
            "DOCX editing requires python-docx (install the 'documents' extra)"
        ) from exc
    if not path.exists():
        raise NotFoundError("document does not exist", details={"path": str(path)})

    document = docx.Document(str(path))
    if not find:
        raise ValidationError("'find' text must not be empty")

    matches = 0
    for paragraph in _iter_docx_paragraphs(document):
        text = paragraph.text
        if find not in text:
            continue
        updated = text.replace(find, replace) if replace_all else text.replace(find, replace, 1)
        matches += text.count(find) if replace_all else 1
        if paragraph.runs:
            paragraph.runs[0].text = updated
            for run in paragraph.runs[1:]:
                run.text = ""
        else:  # pragma: no cover - a paragraph with no runs has no text
            paragraph.add_run(updated)
        if not replace_all and matches >= 1:
            break

    if matches == 0:
        raise NotFoundError(
            "the 'find' text was not found in the document",
            details={"find": find[:80]},
        )
    if expected_replacements is not None and matches != expected_replacements:
        raise ValidationError(
            "replacement count did not match expectation",
            details={"found": matches, "expected": expected_replacements},
        )
    size = _atomic_replace_with(lambda tmp: document.save(str(tmp)), path)
    return EditResult(path=str(path), format="docx", bytes_written=size, replacements=matches)


def _sheet_for_edit(path: Path) -> tuple[Any, Any]:
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover
        raise ConfigurationError(
            "XLSX editing requires openpyxl (install the 'documents' extra)"
        ) from exc
    workbook = openpyxl.load_workbook(str(path))
    return workbook, openpyxl


def edit_xlsx_cell(
    path: Path,
    *,
    sheet: str | None,
    cell: str,
    value: str,
) -> EditResult:
    """Set one cell's value in an XLSX workbook, atomically.

    A spreadsheet is not free text, so an exact-text replacement does not map
    onto it; the caller names a cell (``B4``) and a value. ``sheet`` defaults to
    the active sheet. The whole workbook is written back through a temporary
    file so the replacement is atomic.
    """
    if not path.exists():
        raise NotFoundError("document does not exist", details={"path": str(path)})
    if not cell or not cell.strip():
        raise ValidationError("a target cell is required (e.g. 'B4')")
    workbook, _openpyxl = _sheet_for_edit(path)
    if sheet:
        if sheet not in workbook.sheetnames:
            raise NotFoundError(
                "sheet not found",
                details={"sheet": sheet, "sheets": list(workbook.sheetnames)},
            )
        worksheet = workbook[sheet]
    else:
        worksheet = workbook.active
    try:
        worksheet[cell.strip()] = value
    except (ValueError, TypeError) as exc:
        raise ValidationError("invalid cell reference", details={"cell": cell}) from exc
    size = _atomic_replace_with(lambda tmp: workbook.save(str(tmp)), path)
    return EditResult(path=str(path), format="xlsx", bytes_written=size, replacements=1)


def preview_edit(
    path: Path,
    *,
    find: str,
    replace: str,
    replace_all: bool = False,
    cell: str | None = None,
    sheet: str | None = None,
) -> dict[str, Any]:
    """Read-only description of what an edit would do, for an approval prompt.

    This never writes anything: it reads the target (bounded), counts the
    occurrences of ``find`` and returns a small, human-readable structure — the
    format, the occurrence count, and a short before/after excerpt — so the
    approver sees the concrete change. Missing text or a read-only format is
    reported as an honest ``will_change: false`` note rather than raising, since
    a preview must never block the approval from being raised.
    """
    suffix = path.suffix.lower()
    preview: dict[str, Any] = {
        "operation": "edit_document",
        "path": str(path),
        "format": format_for_suffix(suffix) or suffix.lstrip("."),
        "will_change": False,
    }
    if not path.exists():
        preview["note"] = "file does not exist"
        return preview
    if suffix in (".pdf", ".pptx"):
        preview["note"] = f"{suffix} cannot be edited in place; regenerate it instead"
        return preview
    if suffix == ".xlsx":
        if not cell:
            preview["note"] = "no target cell given"
            return preview
        preview["cell"] = cell
        preview["sheet"] = sheet or "(active)"
        preview["new_value"] = replace[:200]
        preview["will_change"] = True
        return preview
    try:
        text = _read_text_bounded(path, max_chars=2_000_000)
    except (OSError, ValidationError) as exc:
        preview["note"] = f"could not read file: {exc}"
        return preview
    occurrences = text.count(find) if find else 0
    preview["occurrences"] = occurrences
    preview["replace_all"] = replace_all
    preview["will_change"] = occurrences > 0
    if occurrences:
        index = text.find(find)
        start = max(0, index - 40)
        end = min(len(text), index + len(find) + 40)
        preview["excerpt"] = text[start:end]
        preview["find"] = find[:200]
        preview["replace"] = replace[:200]
    else:
        preview["note"] = "'find' text was not found; the edit would fail"
    return preview


def edit_document_in_place(
    path: Path,
    *,
    find: str,
    replace: str,
    replace_all: bool = False,
    expected_replacements: int | None = None,
    cell: str | None = None,
    sheet: str | None = None,
) -> EditResult:
    """Dispatch an in-place edit by the document's format.

    Text formats and DOCX use exact-text replacement; XLSX uses a cell
    reference. PDF and PPTX are refused: neither has a reliable in-place text
    edit, so the caller is told to regenerate them instead of silently writing a
    document that did not change.
    """
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        if cell is None:
            raise ValidationError(
                "editing an .xlsx requires a 'cell' (e.g. 'B4'); text replacement "
                "does not apply to a workbook"
            )
        return edit_xlsx_cell(path, sheet=sheet, cell=cell, value=replace)
    if suffix == ".docx":
        return edit_docx_document(
            path,
            find=find,
            replace=replace,
            replace_all=replace_all,
            expected_replacements=expected_replacements,
        )
    if suffix in (".pdf", ".pptx"):
        raise ValidationError(
            f"format {suffix!r} cannot be edited in place; regenerate it from its "
            "source (e.g. a .md/.docx) instead",
            details={"format": suffix},
        )
    return edit_text_document(
        path,
        find=find,
        replace=replace,
        replace_all=replace_all,
        expected_replacements=expected_replacements,
    )


# --- Export / conversion ---------------------------------------------------


@dataclass(frozen=True)
class ExportResult:
    """The outcome of a real document export."""

    source: str
    destination: str
    source_format: str
    target_format: str
    bytes_written: int
    blocks: int
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "destination": self.destination,
            "source_format": self.source_format,
            "target_format": self.target_format,
            "bytes_written": self.bytes_written,
            "blocks": self.blocks,
            "warnings": list(self.warnings),
        }


def _split_markdown_table(
    block_lines: list[str],
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Parse a Markdown pipe table into headers and rows."""
    rows: list[list[str]] = []
    for line in block_lines:
        if _MD_TABLE_SEP.match(line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows.append(cells)
    if not rows:
        return (), ()
    headers = tuple(rows[0])
    width = len(headers)
    body = tuple(tuple(r[:width] + [""] * max(0, width - len(r))) for r in rows[1:])
    return headers, body


def _sections_to_blocks(document: Document) -> tuple[str, tuple[Block, ...]]:
    """Turn a parsed document into a generation spec (title + blocks).

    The mapping is conservative and lossless where it can be: headings stay
    headings, tables become table blocks, everything else becomes a paragraph or
    a bullet. Content is never dropped or invented.
    """
    title = document.metadata.title or ""
    blocks: list[Block] = []
    index = 0
    sections = list(document.sections)
    while index < len(sections):
        section = sections[index]
        if section.kind == "heading":
            blocks.append(Block(type="heading", text=section.text, level=section.level or 1))
            index += 1
            continue
        if section.kind == "table":
            # A CSV/HTML table section carries pipe- or newline-joined rows; a
            # markdown table is detected by its separator row.
            lines = section.text.splitlines()
            if any(_MD_TABLE_SEP.match(line) for line in lines):
                headers, rows = _split_markdown_table(lines)
                blocks.append(Block(type="table", headers=headers, rows=rows))
            else:
                rows = tuple(tuple(cell.strip() for cell in line.split("|")) for line in lines)
                blocks.append(Block(type="table", rows=rows))
            index += 1
            continue
        if section.kind in ("bullet", "li"):
            items = [line.strip() for line in section.text.splitlines() if line.strip()]
            blocks.append(Block(type="bullet", items=tuple(items)))
            index += 1
            continue
        blocks.append(Block(type="paragraph", text=section.text))
        index += 1
    return title, tuple(blocks)


def _text_lines_to_blocks(text: str) -> tuple[str, tuple[Block, ...]]:
    """Parse a text/Markdown body into heading/paragraph/bullet/table blocks."""
    lines = text.splitlines()
    blocks: list[Block] = []
    title = ""
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(Block(type="paragraph", text="\n".join(paragraph).strip()))
            paragraph.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        heading = _MD_HEADING.match(line)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            text_value = heading.group(2).strip()
            if level == 1 and not title:
                title = text_value
            else:
                blocks.append(Block(type="heading", text=text_value, level=min(6, level)))
            index += 1
            continue
        # A Markdown table: a pipe row immediately followed by a separator row.
        if "|" in line and index + 1 < len(lines) and _MD_TABLE_SEP.match(lines[index + 1]):
            flush_paragraph()
            table_lines = [line]
            index += 1
            while index < len(lines) and "|" in lines[index]:
                table_lines.append(lines[index])
                index += 1
            headers, rows = _split_markdown_table(table_lines)
            blocks.append(Block(type="table", headers=headers, rows=rows))
            continue
        bullet = _MD_BULLET.match(line)
        if bullet:
            flush_paragraph()
            items: list[str] = [bullet.group(1).strip()]
            index += 1
            while index < len(lines):
                nxt = _MD_BULLET.match(lines[index])
                if not nxt:
                    break
                items.append(nxt.group(1).strip())
                index += 1
            blocks.append(Block(type="bullet", items=tuple(items)))
            continue
        paragraph.append(line)
        index += 1
    flush_paragraph()
    return title, tuple(blocks)


def _export_blocks(source: Path, document: Document) -> tuple[str, tuple[Block, ...]]:
    """Derive generation blocks from a parsed document.

    For Markdown/text the raw body is re-parsed so headings, bullets and tables
    survive a conversion (the parser flattens line ranges into sections, which
    would lose the structure). For every other format the parsed sections are
    mapped structurally.
    """
    fmt = document.format
    if fmt in ("markdown", "text"):
        raw = source.read_bytes()[:_MAX_SOURCE_BYTES]
        encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
        text = raw.decode(encoding, errors="replace")
        return _text_lines_to_blocks(text)
    return _sections_to_blocks(document)


def _reject_if_no_text(document: Document, source: Path) -> None:
    if not document.has_text or not document.sections:
        raise ValidationError(
            "the source document has no extractable text to export (an image-only "
            "PDF or a scanned document cannot be converted without OCR)",
            details={"source": str(source), "format": document.format},
        )


def export_document(
    source: Path,
    destination: Path,
    *,
    title: str | None = None,
) -> ExportResult:
    """Convert ``source`` to ``destination``'s format, atomically.

    The source is parsed with the ingestion parsers, mapped onto generation
    blocks, and written with the generation writers, so a converted document is
    a real file in the target format (and Arabic-aware). Unsupported targets and
    empty sources are refused rather than producing an empty file.
    """
    target_suffix = destination.suffix.lower()
    target_format = format_for_suffix(target_suffix)
    if target_format is None:
        raise ValidationError(
            f"unsupported export target: {target_suffix!r}",
            details={"supported": export_extensions()},
        )
    caps = editing_capabilities()
    if not caps.get(target_format, {}).get("exportable", False):
        raise ConfigurationError(
            f"export to {target_format} is not available in this install "
            "(install the matching optional extra)"
        )
    if not source.exists():
        raise NotFoundError("source document does not exist", details={"path": str(source)})
    if source.resolve() == destination.resolve():
        raise ValidationError("the source and destination are the same file")

    document = load_document(source, display_path=str(source))
    _reject_if_no_text(document, source)

    if target_format == "xlsx":
        headers, rows = _tabular_export(document, source)
        if not rows and not headers:
            raise ValidationError(
                "the source has no tabular data to export as a spreadsheet",
                details={"source": str(source)},
            )
        result = _atomic_replace_with(
            lambda tmp: generate_spreadsheet(
                tmp,
                sheet=_sheet_name(document.metadata.title or source.stem),
                headers=headers,
                rows=rows,
            ),
            destination,
        )
        return ExportResult(
            source=str(source),
            destination=str(destination),
            source_format=document.format,
            target_format="xlsx",
            bytes_written=result,
            blocks=len(rows),
            warnings=document.warnings,
        )

    doc_title, blocks = _export_blocks(source, document)
    spec = DocumentSpec(title=title or doc_title or document.metadata.title or "", blocks=blocks)
    if not spec.title and not spec.blocks:
        raise ValidationError(
            "the source has no content to export",
            details={"source": str(source)},
        )
    result = _atomic_replace_with(lambda tmp: generate_document(tmp, spec), destination)
    return ExportResult(
        source=str(source),
        destination=str(destination),
        source_format=document.format,
        target_format=target_format,
        bytes_written=result,
        blocks=len(spec.blocks),
        warnings=document.warnings,
    )


def _sheet_name(name: str) -> str:
    return sanitize_sheet_name(name)


def _tabular_export(
    document: Document, source: Path
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Best-effort tabular extraction for a CSV/XLSX export target.

    CSV sources export row-for-row; a document with table sections exports the
    first table; anything else is refused (the caller raises). This never
    guesses columns from prose.
    """
    if document.format == "csv":
        raw = source.read_bytes()[:_MAX_SOURCE_BYTES]
        import csv
        import io

        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig", errors="replace"))))
        if not rows:
            return (), ()
        width = max(len(r) for r in rows)
        headers = tuple(rows[0] + [""] * (width - len(rows[0])))
        body = tuple(tuple(r + [""] * (width - len(r))) for r in rows[1:])
        return headers, body
    for section in document.sections:
        if section.kind == "table":
            lines = [line for line in section.text.splitlines() if line.strip()]
            if any(_MD_TABLE_SEP.match(line) for line in lines):
                return _split_markdown_table(lines)
            parsed = [tuple(c.strip() for c in line.split("|")) for line in lines]
            if parsed:
                width = max(len(r) for r in parsed)
                headers = tuple(parsed[0] + ("",) * (width - len(parsed[0])))
                body = tuple(tuple(r + ("",) * (width - len(r))) for r in parsed[1:])
                return headers, body
    return (), ()


__all__ = [
    "EditResult",
    "ExportResult",
    "edit_document_in_place",
    "edit_docx_document",
    "edit_text_document",
    "edit_xlsx_cell",
    "editable_extensions",
    "editing_capabilities",
    "export_document",
    "export_extensions",
    "is_editable_extension",
    "preview_edit",
]
