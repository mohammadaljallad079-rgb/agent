"""Goal-aware tool retrieval must surface the specialised tool, not the generic.

The regression this guards: given an Arabic goal to produce a PDF and the whole
registry, a weak local model chose the generic ``write_file``. With the registry
ranked against the goal, the specialised ``generate_document`` is in the
advertised set — so the model has a relevant choice to make.

These are unit tests over the real ranker with real registry specs.
"""

from __future__ import annotations

from arabic_agent.models.base import ToolSpec
from arabic_agent.tools.retrieval import select_relevant_tools


def _spec(name: str, description: str) -> ToolSpec:
    return ToolSpec(name=name, description=description, parameters={"type": "object"})


def _registry() -> list[ToolSpec]:
    """A realistic slice of the built-in registry, English descriptions."""
    return [
        _spec("write_file", "Write UTF-8 text to a file in the workspace, overwrites content."),
        _spec("read_file", "Read a UTF-8 text file from the workspace."),
        _spec("generate_document", "Create a real .pdf, .docx or .pptx document from blocks."),
        _spec("create_spreadsheet", "Create a real spreadsheet (.xlsx or .csv) from rows."),
        _spec("create_presentation", "Create real slides (.pptx) from a title and slides."),
        _spec("email_send", "Send an email message through the configured connector."),
        _spec("print_document", "Send a document to a printer and record the job."),
        _spec("browser_navigate", "Navigate a real browser to a URL."),
        _spec("echo", "Echo text back; a trivial diagnostic tool."),
        _spec("finish", "Finish the run with a final result."),
        _spec("list_directory", "List the entries of a directory in the workspace."),
        _spec("delete_file", "Delete a file from the workspace."),
    ]


def _names(tools) -> set[str]:
    return {spec.name for spec in tools}


def test_pdf_goal_surfaces_the_document_tool_not_only_write_file():
    selected = select_relevant_tools(
        "أنشئ ملف PDF باسم report.pdf يحتوي على عنوان التقرير وفقرة نصية قصيرة.",
        _registry(),
        max_tools=5,
    )
    names = _names(selected)
    assert "generate_document" in names
    assert "finish" in names  # the control tool is never dropped


def test_presentation_goal_surfaces_the_presentation_tool():
    selected = select_relevant_tools(
        "جهّز عرضًا تقديميًا من 3 شرائح عن خطة المبيعات واحفظه في ملف pptx.",
        _registry(),
        max_tools=5,
    )
    assert "create_presentation" in _names(selected)


def test_spreadsheet_goal_surfaces_the_spreadsheet_tool():
    selected = select_relevant_tools("أنشئ جدول اكسل للمبيعات الشهرية.", _registry(), max_tools=5)
    assert "create_spreadsheet" in _names(selected)


def test_english_goal_is_supported_too():
    selected = select_relevant_tools(
        "Send an email to the customer about the invoice.", _registry(), max_tools=5
    )
    assert "email_send" in _names(selected)


def test_orthographic_variants_all_match_the_same_intent():
    for variant in ("إيميل", "ايميل", "الإيميل", "ايميلاً"):
        selected = select_relevant_tools(f"أرسل {variant} للعميل", _registry(), max_tools=6)
        assert "email_send" in _names(selected), variant


def test_a_small_registry_is_returned_whole():
    small = _registry()[:3]
    assert select_relevant_tools("anything", small, max_tools=12) == tuple(small)


def test_an_empty_registry_returns_nothing():
    assert select_relevant_tools("anything", [], max_tools=12) == ()


def test_selection_is_stable_and_bounded():
    tools = _registry()
    first = select_relevant_tools("أنشئ ملف PDF", tools, max_tools=5)
    second = select_relevant_tools("أنشئ ملف PDF", tools, max_tools=5)
    assert first == second
    assert len(first) <= 5
