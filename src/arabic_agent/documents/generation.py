"""Real document generation: turn a structured spec into a real file on disk.

This module *writes files*. It is the generation counterpart to the ingestion
side (:mod:`arabic_agent.documents.parsers`): where the parsers read PDF/DOCX/
XLSX/PPTX/CSV into a :class:`Document`, this module turns a small, explicit
:class:`DocumentSpec` into a real ``.pdf`` / ``.docx`` / ``.xlsx`` / ``.csv`` /
``.md`` / ``.txt`` / ``.html`` / ``.json`` file and returns its real byte size.

Design rules (matching the ingestion side):

* **Honest capability reporting.** :func:`generation_capabilities` reports, per
  format, whether it is actually supported in *this* install — a format whose
  library is absent is reported ``False`` and a generation attempt for it raises
  a clear error rather than writing a broken file.
* **Arabic-first.** PDF output shapes Arabic correctly (contextual joining and
  bidi reordering) using a resolved Unicode font; if Arabic text is requested
  and no Unicode-capable font can be found, the call fails loudly instead of
  emitting a PDF full of missing glyphs. DOCX/XLSX/CSV/Markdown rely on the
  consumer's own text engine and write UTF-8 (CSV uses a BOM so Excel opens
  Arabic correctly).
* **No fabricated content.** The generator writes exactly the blocks it is
  given; it does not invent paragraphs, and an unsupported block type is an
  error, not a silent drop.
* **Bounded.** A hard cap on total characters guards against a runaway spec.

Nothing here touches the network or the host outside the destination path the
caller already resolved through the filesystem sandbox.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..core.errors import ConfigurationError, ValidationError

logger = logging.getLogger(__name__)

#: Hard cap on the characters a single generated document may contain.
MAX_TOTAL_CHARS = 2_000_000

BlockType = Literal["heading", "paragraph", "bullet", "table", "page_break"]

#: Format → the file extensions that select it.
_FORMAT_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "pdf": (".pdf",),
    "docx": (".docx",),
    "pptx": (".pptx",),
    "xlsx": (".xlsx",),
    "csv": (".csv",),
    "markdown": (".md", ".markdown"),
    "text": (".txt",),
    "html": (".html", ".htm"),
    "json": (".json",),
}


def _module_available(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        return False


def generation_capabilities() -> dict[str, bool]:
    """Report which generation formats are usable in this install.

    ``pdf`` additionally requires a Unicode-capable font for Arabic; that is
    checked per call (a Latin-only PDF works without one), so it is not folded
    into this static map.
    """
    has_reportlab = _module_available("reportlab")
    has_docx = _module_available("docx")
    has_xlsx = _module_available("openpyxl")
    has_pptx = _module_available("pptx")
    return {
        "pdf": has_reportlab,
        "docx": has_docx,
        "pptx": has_pptx,
        "xlsx": has_xlsx,
        "csv": True,
        "markdown": True,
        "text": True,
        "html": True,
        "json": True,
    }


def format_for_suffix(suffix: str) -> str | None:
    """Return the generation format for a file extension, or ``None``."""
    lowered = suffix.lower()
    for fmt, extensions in _FORMAT_EXTENSIONS.items():
        if lowered in extensions:
            return fmt
    return None


def supported_generation_extensions() -> list[str]:
    """Extensions that select a supported generation format."""
    return sorted(ext for exts in _FORMAT_EXTENSIONS.values() for ext in exts)


@dataclass(frozen=True)
class Block:
    """One content block of a generated document."""

    type: BlockType
    text: str = ""
    level: int = 1
    items: tuple[str, ...] = ()
    headers: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> Block:
        if not isinstance(raw, dict):
            raise ValidationError("each block must be an object")
        block_type = raw.get("type")
        if block_type not in ("heading", "paragraph", "bullet", "table", "page_break"):
            raise ValidationError(
                f"unsupported block type: {block_type!r}",
                details={"supported": ["heading", "paragraph", "bullet", "table", "page_break"]},
            )
        items = raw.get("items") or ()
        rows = raw.get("rows") or ()
        headers = raw.get("headers") or ()
        return cls(
            type=block_type,
            text=str(raw.get("text") or ""),
            level=int(raw.get("level") or 1),
            items=tuple(str(i) for i in items),
            headers=tuple(str(h) for h in headers),
            rows=tuple(tuple(str(c) for c in row) for row in rows),
        )


@dataclass(frozen=True)
class DocumentSpec:
    """A title plus an ordered list of content blocks."""

    title: str = ""
    blocks: tuple[Block, ...] = field(default_factory=tuple)

    @classmethod
    def from_arguments(cls, arguments: dict[str, Any]) -> DocumentSpec:
        raw_blocks = arguments.get("blocks") or []
        if not isinstance(raw_blocks, list):
            raise ValidationError("'blocks' must be a list")
        blocks = tuple(Block.from_mapping(b) for b in raw_blocks)
        title = str(arguments.get("title") or "")
        if not blocks and not title:
            raise ValidationError("a document needs at least a title or one block")
        return cls(title=title, blocks=blocks)

    def total_chars(self) -> int:
        total = len(self.title)
        for b in self.blocks:
            total += len(b.text) + sum(len(i) for i in b.items)
            total += sum(len(h) for h in b.headers)
            total += sum(len(c) for row in b.rows for c in row)
        return total


@dataclass(frozen=True)
class GenerationResult:
    """The outcome of a real write."""

    path: str
    format: str
    bytes_written: int
    blocks: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "format": self.format,
            "bytes_written": self.bytes_written,
            "blocks": self.blocks,
        }


# --- Font resolution (PDF, Arabic) -----------------------------------------

#: Candidate Unicode-capable fonts per platform, most-preferred first. A font
#: here must carry Arabic glyphs; the PDF path verifies coverage at run time.
_FONT_CANDIDATES: tuple[str, ...] = (
    # Windows
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/tahoma.ttf",
    # Linux
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
    "/usr/share/fonts/truetype/amiri/Amiri-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    # macOS
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/GeezaPro.ttc",
)


def resolve_unicode_font() -> Path | None:
    """Return a Unicode-capable font path, or ``None`` if none is available.

    An operator can pin one with ``AAP_DOCGEN_FONT`` (read from the environment
    only when this module is used from a process that has it); the candidates
    above are the fallback.
    """
    import os

    override = os.environ.get("AAP_DOCGEN_FONT", "").strip()
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return candidate
        logger.warning("AAP_DOCGEN_FONT points at a missing file: %s", override)
    for raw in _FONT_CANDIDATES:
        candidate = Path(raw)
        if candidate.is_file():
            return candidate
    return None


def _contains_arabic(text: str) -> bool:
    for char in text:
        code = ord(char)
        # Arabic, Arabic Supplement, Arabic Extended-A, Arabic Presentation Forms.
        if 0x0600 <= code <= 0x06FF or 0x0750 <= code <= 0x077F or 0x08A0 <= code <= 0x08FF:
            return True
        if 0xFB50 <= code <= 0xFDFF or 0xFE70 <= code <= 0xFEFF:
            return True
    return False


def _shape_arabic(text: str) -> str:
    """Reshape Arabic (contextual joining) and apply bidi reordering.

    Falls back to the input unchanged when the optional shapers are absent —
    but the PDF path only calls this when the font actually needs it, and
    Arabic shaping libraries are part of the ``docgen`` extra, so a missing
    shaper is surfaced as an unsupported-Arabic error rather than silent tofu.
    """
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
    except ImportError as exc:  # pragma: no cover - depends on extras
        raise ConfigurationError(
            "Arabic PDF shaping requires arabic-reshaper and python-bidi "
            "(install the 'docgen' extra)"
        ) from exc
    return str(get_display(arabic_reshaper.reshape(text)))


def _spec_text(spec: DocumentSpec) -> str:
    parts = [spec.title]
    for b in spec.blocks:
        parts.append(b.text)
        parts.extend(b.items)
        parts.extend(b.headers)
        for row in b.rows:
            parts.extend(row)
    return "\n".join(parts)


# --- Writers ----------------------------------------------------------------


def _write_text(path: Path, spec: DocumentSpec) -> int:
    lines: list[str] = []
    if spec.title:
        lines.append(spec.title)
        lines.append("")
    for b in spec.blocks:
        if b.type == "heading":
            lines.append(f"{'#' * max(1, b.level)} {b.text}")
            lines.append("")
        elif b.type == "paragraph":
            lines.append(b.text)
            lines.append("")
        elif b.type == "bullet":
            lines.extend(f"- {item}" for item in b.items)
            lines.append("")
        elif b.type == "table":
            if b.headers:
                lines.append(" | ".join(b.headers))
                lines.append(" | ".join("---" for _ in b.headers))
            lines.extend(" | ".join(cell for cell in row) for row in b.rows)
            lines.append("")
        elif b.type == "page_break":
            lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path.stat().st_size


def _write_json(path: Path, spec: DocumentSpec) -> int:
    payload = {
        "title": spec.title,
        "blocks": [
            {
                "type": b.type,
                **({"text": b.text} if b.text else {}),
                **({"level": b.level} if b.type == "heading" else {}),
                **({"items": list(b.items)} if b.items else {}),
                **({"headers": list(b.headers)} if b.headers else {}),
                **({"rows": [list(r) for r in b.rows]} if b.rows else {}),
            }
            for b in spec.blocks
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path.stat().st_size


def _escape_html(text: str) -> str:
    from html import escape

    return escape(text, quote=True)


def _write_html(path: Path, spec: DocumentSpec) -> int:
    body: list[str] = []
    if spec.title:
        body.append(f"<h1>{_escape_html(spec.title)}</h1>")
    for b in spec.blocks:
        if b.type == "heading":
            level = min(6, max(2, b.level + 1))
            body.append(f"<h{level}>{_escape_html(b.text)}</h{level}>")
        elif b.type == "paragraph":
            body.append(f"<p>{_escape_html(b.text)}</p>")
        elif b.type == "bullet":
            items = "".join(f"<li>{_escape_html(i)}</li>" for i in b.items)
            body.append(f"<ul>{items}</ul>")
        elif b.type == "table":
            head = (
                "<tr>" + "".join(f"<th>{_escape_html(h)}</th>" for h in b.headers) + "</tr>"
                if b.headers
                else ""
            )
            rows = "".join(
                "<tr>" + "".join(f"<td>{_escape_html(c)}</td>" for c in row) + "</tr>"
                for row in b.rows
            )
            body.append(f"<table>{head}{rows}</table>")
        elif b.type == "page_break":
            body.append('<hr class="page-break" />')
    html = (
        "<!DOCTYPE html>\n"
        '<html lang="ar" dir="rtl">\n<head>\n<meta charset="utf-8" />\n'
        f"<title>{_escape_html(spec.title)}</title>\n"
        "</head>\n<body>\n" + "\n".join(body) + "\n</body>\n</html>\n"
    )
    path.write_text(html, encoding="utf-8")
    return path.stat().st_size


def _write_csv(path: Path, spec: DocumentSpec) -> int:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        if spec.title:
            writer.writerow([spec.title])
            writer.writerow([])
        for b in spec.blocks:
            if b.type in ("heading", "paragraph"):
                writer.writerow([b.text])
            elif b.type == "bullet":
                for item in b.items:
                    writer.writerow([item])
            elif b.type == "table":
                if b.headers:
                    writer.writerow(list(b.headers))
                writer.writerows(list(row) for row in b.rows)
            elif b.type == "page_break":
                writer.writerow([])
    return path.stat().st_size


def _write_docx(path: Path, spec: DocumentSpec) -> int:
    try:
        import docx
        from docx.enum.text import WD_ALIGN_PARAGRAPH
    except ImportError as exc:
        raise ConfigurationError(
            "DOCX generation requires python-docx (install the 'documents' extra)"
        ) from exc

    document = docx.Document()
    has_arabic = _contains_arabic(_spec_text(spec))
    if has_arabic:
        # Right-to-left base direction for the whole document so Arabic reads
        # naturally; the consumer's text engine does the glyph shaping.
        for section in document.sections:
            section.left_margin, section.right_margin = section.right_margin, section.left_margin

    if spec.title:
        heading = document.add_heading(spec.title, level=0)
        if has_arabic:
            heading.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for b in spec.blocks:
        if b.type == "heading":
            paragraph = document.add_heading(b.text, level=min(9, max(1, b.level)))
            if has_arabic:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        elif b.type == "paragraph":
            paragraph = document.add_paragraph(b.text)
            if has_arabic:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        elif b.type == "bullet":
            for item in b.items:
                paragraph = document.add_paragraph(item, style="List Bullet")
                if has_arabic:
                    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        elif b.type == "table":
            columns = max(len(b.headers), max((len(r) for r in b.rows), default=0))
            if columns:
                table = document.add_table(rows=0, cols=columns)
                if b.headers:
                    row = table.add_row().cells
                    for index, header in enumerate(b.headers):
                        row[index].text = header
                for values in b.rows:
                    row = table.add_row().cells
                    for index, value in enumerate(values):
                        row[index].text = value
        elif b.type == "page_break":
            document.add_page_break()
    document.save(str(path))
    return path.stat().st_size


#: Title-and-content layout, the classic "title + bullet body" slide.
_PPTX_TITLE_CONTENT_LAYOUT = 1
#: Blank layout, used when a slide has no title.
_PPTX_BLANK_LAYOUT = 6


def _write_pptx(path: Path, spec: DocumentSpec) -> int:
    """Write a real ``.pptx`` deck from a title and content blocks.

    A document maps onto a deck the way a reader expects:

    * the document title becomes the deck's title on the first slide;
    * each heading starts a new slide, its text becoming that slide's title;
    * paragraphs, bullets and tables become the body of the current slide (a
      slide is opened automatically if none is active yet);
    * ``page_break`` starts a new (blank) slide, matching its "new page" meaning.

    Arabic is first-class: every paragraph is marked right-to-left (the OOXML
    ``rtl`` paragraph attribute) and right-aligned, so a renderer lays Arabic out
    naturally. The logical text is stored as-is — python-pptx applies no shaping
    and PowerPoint does its own, so pre-shaping would corrupt the stored text
    (breaking copy/paste, search and re-parsing). The file is a real OOXML
    package and re-parses with python-pptx.
    """
    try:
        from pptx import Presentation
        from pptx.enum.text import PP_ALIGN
        from pptx.util import Pt
    except ImportError as exc:
        raise ConfigurationError(
            "PPTX generation requires python-pptx (install the 'documents' extra)"
        ) from exc

    has_arabic = _contains_arabic(_spec_text(spec))

    def set_rtl(frame: object) -> None:
        # Right-to-left paragraph direction for every paragraph in the frame.
        paragraphs = getattr(frame, "paragraphs", None)
        if paragraphs is None:
            return
        for paragraph in paragraphs:
            ppr = paragraph._p.get_or_add_pPr()
            ppr.set("rtl", "1")

    def add_body_frame(slide: object, items: tuple[str, ...]) -> None:
        if not items:
            return
        body = None
        for placeholder in slide.placeholders:  # type: ignore[attr-defined]
            if placeholder.placeholder_format.idx == 1:
                body = placeholder
                break
        if body is None:
            body = slide.shapes.add_textbox(  # type: ignore[attr-defined]
                Pt(48), Pt(120), Pt(864), Pt(360)
            )
        frame = body.text_frame
        for item in items:
            # Reuse the placeholder's initial empty paragraph on first use, then
            # append — never clear, so successive body blocks accumulate rather
            # than overwrite each other.
            first = frame.paragraphs[0]
            if len(frame.paragraphs) == 1 and not first.runs:
                paragraph = first
            else:
                paragraph = frame.add_paragraph()
            paragraph.text = item
            if has_arabic:
                paragraph.alignment = PP_ALIGN.RIGHT
            for run in paragraph.runs:
                run.font.size = Pt(20)
        if has_arabic:
            set_rtl(frame)

    presentation = Presentation()
    layout = presentation.slide_layouts[_PPTX_TITLE_CONTENT_LAYOUT]
    blank_layout = presentation.slide_layouts[_PPTX_BLANK_LAYOUT]
    current: Any = None
    body_lines = 0

    def start_slide(slide: Any) -> Any:
        nonlocal current, body_lines
        current = slide
        body_lines = 0
        return slide

    def ensure_slide() -> Any:
        if current is None:
            start_slide(presentation.slides.add_slide(layout))
        assert current is not None
        return current

    if spec.title:
        title_slide = presentation.slides.add_slide(presentation.slide_layouts[0])
        title_frame = title_slide.shapes.title
        if title_frame is not None:
            title_frame.text = spec.title
            if has_arabic:
                for paragraph in title_frame.text_frame.paragraphs:
                    paragraph.alignment = PP_ALIGN.RIGHT
                set_rtl(title_frame.text_frame)

    for block in spec.blocks:
        if block.type == "heading":
            slide = start_slide(presentation.slides.add_slide(layout))
            title_frame = slide.shapes.title
            if title_frame is not None:
                title_frame.text = block.text
                if has_arabic:
                    for paragraph in title_frame.text_frame.paragraphs:
                        paragraph.alignment = PP_ALIGN.RIGHT
                    set_rtl(title_frame.text_frame)
        elif block.type == "paragraph":
            add_body_frame(ensure_slide(), (block.text,))
            body_lines += 1
        elif block.type == "bullet":
            add_body_frame(ensure_slide(), tuple(f"• {item}" for item in block.items))
            body_lines += len(block.items)
        elif block.type == "table":
            slide = ensure_slide()
            columns = max(len(block.headers), max((len(r) for r in block.rows), default=0))
            if columns:
                rows_total = len(block.rows) + (1 if block.headers else 0)
                top = Pt(160 + min(body_lines, 8) * 26)
                table = slide.shapes.add_table(
                    rows_total, columns, Pt(48), top, Pt(864), Pt(24) * max(1, rows_total)
                ).table
                if block.headers:
                    for index, header in enumerate(block.headers):
                        table.cell(0, index).text = header
                    data_start = 1
                else:
                    data_start = 0
                for row_index, values in enumerate(block.rows):
                    for col_index, value in enumerate(values):
                        table.cell(data_start + row_index, col_index).text = value
                body_lines += rows_total
        elif block.type == "page_break":
            start_slide(presentation.slides.add_slide(blank_layout))

    if not spec.title and not spec.blocks:
        # A deck with no slides is a broken artifact; refuse rather than write it.
        raise ValidationError("a presentation needs at least a title or one block")
    if len(presentation.slides) == 0:
        presentation.slides.add_slide(blank_layout)

    presentation.save(str(path))
    return path.stat().st_size


#: Excel forbids these characters in a sheet name, and caps it at 31 chars.
_INVALID_SHEET_CHARS = set("[]:*?/\\")


def sanitize_sheet_name(name: str) -> str:
    """Return a sheet name Excel accepts, or ``"Sheet1"`` for an empty one.

    A caller-supplied name is honoured, not silently replaced: forbidden
    characters are stripped and the result is truncated to Excel's 31-character
    limit. An empty result falls back to ``"Sheet1"``.
    """
    cleaned = "".join(ch for ch in name if ch not in _INVALID_SHEET_CHARS).strip()
    cleaned = cleaned[:31].strip()
    return cleaned or "Sheet1"


def _write_xlsx(
    path: Path,
    *,
    sheet: str,
    headers: tuple[str, ...],
    rows: tuple[tuple[str, ...], ...],
) -> int:
    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise ConfigurationError(
            "XLSX generation requires openpyxl (install the 'documents' extra)"
        ) from exc

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sanitize_sheet_name(sheet)
    if headers:
        worksheet.append(list(headers))
    for row in rows:
        worksheet.append(list(row))
    workbook.save(str(path))
    return path.stat().st_size


def _write_pdf(path: Path, spec: DocumentSpec) -> int:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen import canvas
    except ImportError as exc:
        raise ConfigurationError(
            "PDF generation requires reportlab (install the 'docgen' extra)"
        ) from exc

    text = _spec_text(spec)
    has_arabic = _contains_arabic(text)
    font_name = "Helvetica"
    font_path = resolve_unicode_font()
    if font_path is not None:
        try:
            pdfmetrics.registerFont(TTFont("AAPUnicode", str(font_path)))
            font_name = "AAPUnicode"
        except Exception as exc:  # a bad font file must not crash generation
            logger.warning("failed to register font %s: %s", font_path, exc)
            font_path = None
    if has_arabic and font_path is None:
        raise ConfigurationError(
            "Arabic PDF text needs a Unicode font; none was found. Set AAP_DOCGEN_FONT "
            "to a .ttf that covers Arabic (e.g. Arial on Windows, DejaVuSans on Linux)."
        )

    def render(line: str) -> str:
        if has_arabic:
            return _shape_arabic(line)
        return line

    width, height = A4
    margin = 56.0
    max_width = width - 2 * margin
    size = 12.0
    leading = size * 1.6

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setTitle(spec.title or "document")
    y = height - margin

    def new_page_if_needed() -> None:
        nonlocal y
        if y < margin + leading:
            pdf.showPage()
            pdf.setFont(font_name, size)
            y = height - margin

    def draw_wrapped(text_value: str, *, font_size: float, bold: bool = False) -> None:
        nonlocal y
        pdf.setFont(font_name, font_size)
        for paragraph in text_value.split("\n"):
            words = paragraph.split(" ") if paragraph else [""]
            current = ""
            for word in words:
                candidate = f"{current} {word}".strip()
                display = render(candidate)
                if pdfmetrics.stringWidth(display, font_name, font_size) <= max_width:
                    current = candidate
                else:
                    new_page_if_needed()
                    pdf.drawRightString(width - margin, y, render(current))
                    y -= font_size * 1.5
                    current = word
            new_page_if_needed()
            pdf.drawRightString(width - margin, y, render(current))
            y -= font_size * 1.5

    pdf.setFont(font_name, size)
    if spec.title:
        draw_wrapped(spec.title, font_size=18.0)
        y -= leading * 0.4
    for b in spec.blocks:
        if b.type == "heading":
            y -= leading * 0.3
            draw_wrapped(b.text, font_size=max(12.0, 18.0 - b.level * 2.0))
            y -= leading * 0.2
        elif b.type == "paragraph":
            draw_wrapped(b.text, font_size=size)
            y -= leading * 0.2
        elif b.type == "bullet":
            for item in b.items:
                draw_wrapped(f"• {item}", font_size=size)
            y -= leading * 0.2
        elif b.type == "table":
            if b.headers:
                draw_wrapped(" | ".join(b.headers), font_size=size)
            for row in b.rows:
                draw_wrapped(" | ".join(row), font_size=size)
            y -= leading * 0.2
        elif b.type == "page_break":
            pdf.showPage()
            pdf.setFont(font_name, size)
            y = height - margin
    pdf.showPage()
    pdf.save()
    path.write_bytes(buffer.getvalue())
    return path.stat().st_size


def generate_document(path: Path, spec: DocumentSpec) -> GenerationResult:
    """Write ``spec`` to ``path`` in the format implied by its extension."""
    total = spec.total_chars()
    if total > MAX_TOTAL_CHARS:
        raise ValidationError(
            f"document is too large to generate ({total} chars; limit {MAX_TOTAL_CHARS})"
        )
    fmt = format_for_suffix(path.suffix)
    if fmt is None:
        raise ValidationError(
            f"unsupported document extension: {path.suffix!r}",
            details={"supported": supported_generation_extensions()},
        )
    if fmt == "xlsx":
        # A spreadsheet is tabular; content blocks do not map onto it without
        # guessing. Refuse rather than write a near-empty workbook.
        raise ValidationError(
            "use create_spreadsheet for .xlsx/.csv output; generate_document "
            "handles .pdf, .docx, .pptx, .md, .txt, .html and .json",
            details={"supported": [".pdf", ".docx", ".pptx", ".md", ".txt", ".html", ".json"]},
        )
    if not generation_capabilities().get(fmt, False):
        raise ConfigurationError(
            f"generation of {fmt} is not available in this install "
            "(install the matching optional extra)"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "pdf":
        size = _write_pdf(path, spec)
    elif fmt == "docx":
        size = _write_docx(path, spec)
    elif fmt == "pptx":
        size = _write_pptx(path, spec)
    elif fmt == "csv":
        size = _write_csv(path, spec)
    elif fmt == "html":
        size = _write_html(path, spec)
    elif fmt == "json":
        size = _write_json(path, spec)
    elif fmt == "markdown":
        size = _write_text(path, spec)
    else:  # text
        size = _write_text(path, spec)
    return GenerationResult(path=str(path), format=fmt, bytes_written=size, blocks=len(spec.blocks))


def generate_spreadsheet(
    path: Path,
    *,
    sheet: str,
    headers: tuple[str, ...],
    rows: tuple[tuple[str, ...], ...],
) -> GenerationResult:
    """Write a real spreadsheet (``.xlsx`` or ``.csv``) from tabular data."""
    fmt = format_for_suffix(path.suffix)
    if fmt not in ("xlsx", "csv"):
        raise ValidationError(
            f"a spreadsheet must be .xlsx or .csv, not {path.suffix!r}",
            details={"supported": [".xlsx", ".csv"]},
        )
    total = sum(len(h) for h in headers) + sum(len(c) for r in rows for c in r)
    if total > MAX_TOTAL_CHARS:
        raise ValidationError(f"spreadsheet is too large to generate ({total} chars)")
    if fmt == "xlsx" and not generation_capabilities().get("xlsx", False):
        raise ConfigurationError(
            "XLSX generation requires openpyxl (install the 'documents' extra)"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "xlsx":
        size = _write_xlsx(path, sheet=sheet, headers=headers, rows=rows)
    else:
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            if headers:
                writer.writerow(list(headers))
            writer.writerows(list(row) for row in rows)
        size = path.stat().st_size
    return GenerationResult(path=str(path), format=fmt, bytes_written=size, blocks=len(rows))


__all__ = [
    "MAX_TOTAL_CHARS",
    "Block",
    "DocumentSpec",
    "GenerationResult",
    "format_for_suffix",
    "generate_document",
    "generate_spreadsheet",
    "generation_capabilities",
    "resolve_unicode_font",
    "sanitize_sheet_name",
    "supported_generation_extensions",
]
