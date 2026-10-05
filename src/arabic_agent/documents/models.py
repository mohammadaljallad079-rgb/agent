"""Document data contracts.

A parsed document is represented as an ordered list of *sections*, each carrying
**provenance**: the file it came from and a locator that identifies the exact
place (page, slide, sheet + range, heading, line range). The agent reasons over
sections and can cite where a fact came from, which is what makes its answers
verifiable rather than invented.

Nothing here is format-specific; parsers produce these types and the rest of the
platform only ever sees them.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Provenance(BaseModel):
    """Where a piece of document content came from."""

    model_config = ConfigDict(frozen=True)

    #: The file path as the caller supplied it (workspace-relative or absolute).
    path: str
    #: A short, human-readable locator, e.g. "page 3", "sheet 'Sales' A1:C10".
    locator: str
    #: 1-based page number (PDF, where available).
    page: int | None = None
    #: 1-based slide number (PPTX).
    slide: int | None = None
    #: Sheet name (XLSX).
    sheet: str | None = None
    #: Cell/row range, e.g. "A1:C10" (XLSX) or "rows 1-50" (CSV).
    range: str | None = None
    #: 1-based line number or start of a line range (text formats).
    line: int | None = None
    #: Inclusive end line for a line range.
    end_line: int | None = None

    def describe(self) -> str:
        return f"{self.path} ({self.locator})"


class DocumentSection(BaseModel):
    """One addressable, order-preserving unit of document content."""

    model_config = ConfigDict(frozen=True)

    id: str
    #: paragraph | heading | table | sheet | slide | page | text | row | note
    kind: str
    text: str
    #: Heading level (1-6) where kind == "heading"; else None.
    level: int | None = None
    provenance: Provenance
    #: Cheap token estimate so the context manager can budget without re-counting.
    tokens: int = 0


class DocumentMetadata(BaseModel):
    """Format-level metadata (never secrets; parsers keep this to safe fields)."""

    model_config = ConfigDict(frozen=True)

    page_count: int | None = None
    slide_count: int | None = None
    sheet_count: int | None = None
    paragraph_count: int | None = None
    row_count: int | None = None
    column_count: int | None = None
    title: str | None = None
    author: str | None = None
    created: str | None = None
    extra: dict[str, object] = Field(default_factory=dict)


class Document(BaseModel):
    """A parsed document: metadata plus ordered, provenance-bearing sections."""

    model_config = ConfigDict(frozen=True)

    path: str
    format: str
    metadata: DocumentMetadata = Field(default_factory=DocumentMetadata)
    sections: tuple[DocumentSection, ...] = ()
    #: True when the parser stopped early (size/page/section caps).
    truncated: bool = False
    #: True when the format parsed but yielded no usable text (e.g. a scanned
    #: image-only PDF). The caller must not claim extraction succeeded.
    has_text: bool = True
    #: Honest, non-fatal notes (e.g. "OCR not attempted", "sheet truncated").
    warnings: tuple[str, ...] = ()

    @property
    def section_count(self) -> int:
        return len(self.sections)

    def total_tokens(self) -> int:
        return sum(section.tokens for section in self.sections)

    def by_id(self, section_id: str) -> DocumentSection | None:
        for section in self.sections:
            if section.id == section_id:
                return section
        return None
