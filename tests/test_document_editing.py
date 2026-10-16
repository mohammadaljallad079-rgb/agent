"""Document editing and export: real files, verified by re-reading them.

Every test writes a real document, edits or converts it with the *real* code,
and verifies the result by re-parsing the output with the ingestion parsers.
Nothing asserts a file exists without checking its content. Unsupported or
impossible requests must fail loudly rather than write a broken file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arabic_agent.core.errors import (
    ConfigurationError,
    NotFoundError,
    ValidationError,
)
from arabic_agent.documents.editing import (
    edit_document_in_place,
    edit_text_document,
    edit_xlsx_cell,
    editable_extensions,
    editing_capabilities,
    export_document,
    export_extensions,
    preview_edit,
)
from arabic_agent.documents.generation import (
    Block,
    DocumentSpec,
    generate_document,
    generate_spreadsheet,
    generation_capabilities,
)
from arabic_agent.documents.loader import load_document

from .conftest import requires_html_parser

_CAPS = generation_capabilities()
requires_docx = pytest.mark.skipif(not _CAPS["docx"], reason="python-docx not installed")
requires_xlsx = pytest.mark.skipif(not _CAPS["xlsx"], reason="openpyxl not installed")
requires_pdf = pytest.mark.skipif(not _CAPS["pdf"], reason="reportlab not installed")


# --- editing: text formats -------------------------------------------------


def test_edit_text_document_replaces_and_verifies(tmp_path: Path) -> None:
    path = tmp_path / "note.md"
    path.write_text("# العنوان\n\nمرحبا بالعالم\n", encoding="utf-8")  # noqa: RUF001
    result = edit_text_document(path, find="مرحبا", replace="أهلاً")
    assert result.replacements == 1
    assert result.bytes_written == path.stat().st_size
    assert "أهلاً بالعالم" in path.read_text(encoding="utf-8")


def test_edit_text_document_refuses_missing_text(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("hello", encoding="utf-8")
    with pytest.raises(NotFoundError):
        edit_text_document(path, find="absent", replace="x")


def test_edit_text_document_refuses_ambiguous_match(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("a a", encoding="utf-8")
    with pytest.raises(ValidationError):
        edit_text_document(path, find="a", replace="b")
    # Nothing was written.
    assert path.read_text(encoding="utf-8") == "a a"


def test_edit_text_document_replace_all(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("a a", encoding="utf-8")
    result = edit_text_document(path, find="a", replace="b", replace_all=True)
    assert result.replacements == 2
    assert path.read_text(encoding="utf-8") == "b b"


def test_edit_text_document_expected_count_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("a b", encoding="utf-8")
    with pytest.raises(ValidationError):
        edit_text_document(path, find="a", replace="c", expected_replacements=2)


def test_edit_text_document_preserves_bom(tmp_path: Path) -> None:
    path = tmp_path / "note.csv"
    path.write_bytes(b"\xef\xbb\xbfname,value\nA,1\n")
    edit_text_document(path, find="A", replace="B")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert "B,1" in path.read_text(encoding="utf-8-sig")


def test_edit_refuses_pdf_in_place(tmp_path: Path) -> None:
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    with pytest.raises(ValidationError):
        edit_document_in_place(path, find="a", replace="b")


def test_edit_xlsx_requires_cell(tmp_path: Path) -> None:
    path = tmp_path / "book.xlsx"
    path.write_bytes(b"not really xlsx")
    with pytest.raises(ValidationError):
        edit_document_in_place(path, find="a", replace="b")


# --- editing: DOCX / XLSX --------------------------------------------------


@requires_docx
def test_edit_docx_document_updates_paragraph(tmp_path: Path) -> None:
    path = tmp_path / "doc.docx"
    spec = DocumentSpec(
        title="تقرير",
        blocks=(
            Block(type="paragraph", text="السطر الأول"),
            Block(type="paragraph", text="الثاني"),
        ),
    )
    generate_document(path, spec)
    result = edit_document_in_place(path, find="الأول", replace="البديل")
    assert result.format == "docx"
    document = load_document(path, display_path=str(path))
    text = "\n".join(section.text for section in document.sections)
    assert "البديل" in text


@requires_docx
def test_edit_docx_document_refuses_missing(tmp_path: Path) -> None:
    path = tmp_path / "doc.docx"
    generate_document(path, DocumentSpec(title="t", blocks=(Block(type="paragraph", text="x"),)))
    with pytest.raises(NotFoundError):
        edit_document_in_place(path, find="zzz", replace="y")


@requires_xlsx
def test_edit_xlsx_cell_sets_value(tmp_path: Path) -> None:
    path = tmp_path / "book.xlsx"
    generate_spreadsheet(path, sheet="البيانات", headers=("الاسم", "القيمة"), rows=(("A", "1"),))
    result = edit_xlsx_cell(path, sheet="البيانات", cell="B2", value="42")
    assert result.replacements == 1
    document = load_document(path, display_path=str(path))
    assert "42" in "\n".join(section.text for section in document.sections)


@requires_xlsx
def test_edit_xlsx_cell_missing_sheet(tmp_path: Path) -> None:
    path = tmp_path / "book.xlsx"
    generate_spreadsheet(path, sheet="Sheet1", headers=("h",), rows=(("v",),))
    with pytest.raises(NotFoundError):
        edit_xlsx_cell(path, sheet="Nope", cell="A1", value="x")


# --- export / conversion ---------------------------------------------------


@requires_html_parser
def test_export_markdown_to_html_keeps_structure(tmp_path: Path) -> None:
    source = tmp_path / "src.md"
    source.write_text(
        "# تقرير\n\n## القسم\n\nنص عربي.\n\n- بند أول\n- بند ثان\n",
        encoding="utf-8",
    )
    destination = tmp_path / "out.html"
    result = export_document(source, destination)
    assert result.target_format == "html"
    assert destination.exists()
    document = load_document(destination, display_path=str(destination))
    text = "\n".join(section.text for section in document.sections)
    assert "تقرير" in text
    assert "بند أول" in text
    assert result.blocks >= 3


def test_export_markdown_to_text(tmp_path: Path) -> None:
    source = tmp_path / "src.md"
    source.write_text("# عنوان\n\nفقرة.\n", encoding="utf-8")
    destination = tmp_path / "out.txt"
    result = export_document(source, destination)
    assert result.target_format == "text"
    assert "عنوان" in destination.read_text(encoding="utf-8")


def test_export_markdown_table_to_csv(tmp_path: Path) -> None:
    source = tmp_path / "table.md"
    source.write_text(
        "| البند | السعر |\n| --- | --- |\n| خدمة | 100 |\n",
        encoding="utf-8",
    )
    destination = tmp_path / "out.csv"
    result = export_document(source, destination)
    assert result.target_format == "csv"
    text = destination.read_text(encoding="utf-8-sig")
    assert "البند" in text
    assert "خدمة" in text


@requires_docx
def test_export_markdown_to_docx_round_trips(tmp_path: Path) -> None:
    source = tmp_path / "src.md"
    source.write_text("# عنوان\n\n## فرع\n\nنص.\n", encoding="utf-8")
    destination = tmp_path / "out.docx"
    export_document(source, destination)
    document = load_document(destination, display_path=str(destination))
    text = "\n".join(section.text for section in document.sections)
    assert "عنوان" in text and "فرع" in text


@requires_pdf
def test_export_to_pdf_contains_text(tmp_path: Path) -> None:
    source = tmp_path / "src.md"
    source.write_text("# Report\n\nHello world.\n", encoding="utf-8")
    destination = tmp_path / "out.pdf"
    export_document(source, destination)
    document = load_document(destination, display_path=str(destination))
    assert document.has_text
    assert "Hello world" in "\n".join(section.text for section in document.sections)


@requires_xlsx
def test_export_csv_to_xlsx_round_trips(tmp_path: Path) -> None:
    source = tmp_path / "data.csv"
    source.write_text("name,value\nA,1\nB,2\n", encoding="utf-8")
    destination = tmp_path / "out.xlsx"
    result = export_document(source, destination)
    assert result.target_format == "xlsx"
    document = load_document(destination, display_path=str(destination))
    text = "\n".join(section.text for section in document.sections)
    assert "name" in text and "A" in text


def test_export_refuses_unknown_target(tmp_path: Path) -> None:
    source = tmp_path / "src.md"
    source.write_text("# t\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        export_document(source, tmp_path / "out.xyz")


def test_export_refuses_same_file(tmp_path: Path) -> None:
    source = tmp_path / "src.md"
    source.write_text("# t\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        export_document(source, source)


def test_export_refuses_empty_source(tmp_path: Path) -> None:
    source = tmp_path / "empty.md"
    source.write_text("   \n\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        export_document(source, tmp_path / "out.txt")


def test_export_refuses_missing_source(tmp_path: Path) -> None:
    with pytest.raises(NotFoundError):
        export_document(tmp_path / "nope.md", tmp_path / "out.txt")


def test_export_does_not_leave_temporary_file(tmp_path: Path) -> None:
    source = tmp_path / "src.md"
    source.write_text("# عنوان\n", encoding="utf-8")
    export_document(source, tmp_path / "out.html")
    leftovers = [p.name for p in tmp_path.iterdir() if ".aap-tmp" in p.name]
    assert leftovers == []


def test_export_target_format_unavailable_reports_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "src.md"
    source.write_text("# t\n", encoding="utf-8")
    monkeypatch.setattr(
        "arabic_agent.documents.editing.editing_capabilities",
        lambda: {"pdf": {"editable": False, "exportable": False}},
    )
    with pytest.raises(ConfigurationError):
        export_document(source, tmp_path / "out.pdf")


def test_capabilities_and_extensions_are_consistent() -> None:
    caps = editing_capabilities()
    for ext in editable_extensions():
        assert ext.startswith(".")
    for ext in export_extensions():
        assert ext.startswith(".")
    assert caps["pdf"]["editable"] is False
    assert caps["pdf"]["exportable"] == generation_capabilities()["pdf"]
    assert caps["text"]["editable"] is True


# --- preview (read-only, drives the approval prompt) -----------------------


def test_preview_edit_reports_occurrences_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "note.md"
    path.write_text("مرحبا بالعالم\n", encoding="utf-8")
    before = path.read_bytes()
    preview = preview_edit(path, find="مرحبا", replace="أهلاً")
    assert preview["will_change"] is True
    assert preview["occurrences"] == 1
    assert "excerpt" in preview
    # A preview must never touch the file.
    assert path.read_bytes() == before


def test_preview_edit_reports_missing_text_honestly(tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("hello", encoding="utf-8")
    preview = preview_edit(path, find="absent", replace="x")
    assert preview["will_change"] is False
    assert preview["occurrences"] == 0
    assert "note" in preview


def test_preview_edit_refuses_read_only_formats(tmp_path: Path) -> None:
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.4")
    preview = preview_edit(path, find="a", replace="b")
    assert preview["will_change"] is False
    assert "regenerate" in preview["note"]


def test_preview_edit_reports_missing_file(tmp_path: Path) -> None:
    preview = preview_edit(tmp_path / "nope.md", find="a", replace="b")
    assert preview["will_change"] is False
    assert "does not exist" in preview["note"]


def test_preview_edit_xlsx_reports_cell(tmp_path: Path) -> None:
    path = tmp_path / "book.xlsx"
    path.write_bytes(b"x")
    preview = preview_edit(path, find="", replace="42", cell="B2", sheet="Data")
    assert preview["will_change"] is True
    assert preview["cell"] == "B2"
    assert preview["sheet"] == "Data"


# --- human-in-the-loop wiring through the executor -------------------------


def _executor_with(tmp_path: Path, tools: tuple[type, ...]):
    from arabic_agent.agent.executor import ToolExecutor
    from arabic_agent.audit.base import AuditLog
    from arabic_agent.audit.memory_sink import InMemoryAuditSink
    from arabic_agent.permissions.approvals import InMemoryApprovalStore
    from arabic_agent.permissions.policy import PolicyEngine
    from arabic_agent.tools.registry import ToolRegistry

    audit = AuditLog([InMemoryAuditSink()])
    return ToolExecutor(
        registry=ToolRegistry([cls() for cls in tools]),
        policy=PolicyEngine(),
        approvals=InMemoryApprovalStore(),
        audit=audit,
    )


def _ctx(tmp_path: Path):
    from arabic_agent.tools.base import ToolContext

    return ToolContext(tenant_id="t", user_id="u", run_id="r", workspace_dir=str(tmp_path))


def _principal():
    from arabic_agent.permissions.base import Principal

    return Principal(tenant_id="t", user_id="u")


async def test_edit_document_approval_carries_preview(tmp_path: Path) -> None:
    from arabic_agent.agent.executor import ExecutionOutcomeKind
    from arabic_agent.tools.builtin.docgen import EditDocumentTool

    target = tmp_path / "note.md"
    target.write_text("قديم\n", encoding="utf-8")
    executor = _executor_with(tmp_path, (EditDocumentTool,))
    outcome = await executor.execute(
        tool_name="edit_document",
        arguments={"path": "note.md", "find": "قديم", "replace": "جديد"},
        context=_ctx(tmp_path),
        principal=_principal(),
    )
    assert outcome.kind is ExecutionOutcomeKind.APPROVAL_REQUIRED
    assert outcome.approval is not None
    assert outcome.approval.preview["operation"] == "edit_document"
    assert outcome.approval.preview["occurrences"] == 1
    # Approval means the file was NOT changed.
    assert target.read_text(encoding="utf-8") == "قديم\n"


async def test_write_file_approval_preview_flags_overwrite(tmp_path: Path) -> None:
    from arabic_agent.agent.executor import ExecutionOutcomeKind
    from arabic_agent.tools.builtin.filesystem import WriteFileTool

    target = tmp_path / "existing.txt"
    target.write_text("old content", encoding="utf-8")
    executor = _executor_with(tmp_path, (WriteFileTool,))
    outcome = await executor.execute(
        tool_name="write_file",
        arguments={"path": "existing.txt", "content": "new"},
        context=_ctx(tmp_path),
        principal=_principal(),
    )
    assert outcome.kind is ExecutionOutcomeKind.APPROVAL_REQUIRED
    assert outcome.approval is not None
    assert outcome.approval.preview["overwrites_existing"] is True
    assert outcome.approval.preview["existing_bytes"] == len(b"old content")
    assert target.read_text(encoding="utf-8") == "old content"
