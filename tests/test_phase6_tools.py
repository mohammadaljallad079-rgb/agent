"""Phase 6: the document / project tools driven through the real agent.

These use a fake *model* only to choose which tool to call; the tools, sandbox,
policy engine, approval store and audit log are all real. They prove the agent
can inspect a document, retrieve a section with provenance, inspect a project
and surface a secret warning — end to end, not merely that a function exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arabic_agent.audit.base import AuditEventType
from arabic_agent.core.enums import RunStatus
from arabic_agent.permissions.base import Principal
from tests.helpers import (
    is_planner,
    is_verifier,
    plan_json,
    text_response,
    tool_response,
    verify_json,
)

pytestmark = pytest.mark.asyncio


def _principal() -> Principal:
    return Principal(tenant_id="t1", user_id="u1")


def _responder(tool_name: str, arguments: dict, *, plan: str, verification: str | None = None):
    def respond(request, _n):
        if is_verifier(request):
            return text_response(verification or verify_json(True, "تم"))
        if is_planner(request):
            return text_response(plan)
        return tool_response(tool_name, arguments, call_id="c1")

    return respond


async def test_agent_inspects_document_and_gets_provenance(make_platform, tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "requirements.txt").write_text(
        "المتطلبات:\nيجب دعم اللغة العربية.\nيجب أن يعمل دون إنترنت.\n", encoding="utf-8"
    )
    responder = _responder(
        "inspect_document",
        {"path": "requirements.txt"},
        plan=plan_json(("افحص المستند", "inspect_document")),
    )
    platform = make_platform(responder)
    result = await platform.build_runner().run(
        principal=_principal(), goal="افحص المتطلبات", workspace_dir=str(docs)
    )
    assert result.status is RunStatus.COMPLETED
    assert result.verified is True
    # The document.parsed audit event was recorded.
    events = [
        e.type for e in await platform.audit.list_for_run(tenant_id="t1", run_id=result.run_id)
    ]
    assert AuditEventType.DOCUMENT_PARSED in events


async def test_agent_reads_and_searches_document_with_provenance(
    make_platform, tmp_path: Path
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "contract.txt").write_text(
        "\n".join(
            [f"بند {i} عام" for i in range(50)]
            + ["مدة الضمان 24 شهرًا للأجهزة"]
            + [f"خاتمة {i}" for i in range(10)]
        ),
        encoding="utf-8",
    )
    responder = _responder(
        "search_document",
        {"path": "contract.txt", "query": "الضمان"},
        plan=plan_json(("ابحث عن الضمان", "search_document")),
    )
    platform = make_platform(responder)
    result = await platform.build_runner().run(
        principal=_principal(), goal="ما مدة الضمان؟", workspace_dir=str(docs)
    )
    assert result.status is RunStatus.COMPLETED
    events = [
        e.type for e in await platform.audit.list_for_run(tenant_id="t1", run_id=result.run_id)
    ]
    assert AuditEventType.DOCUMENT_RETRIEVED in events


async def test_agent_inspects_project(make_platform, tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    (project / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (project / "pyproject.toml").write_text('[project]\nname="x"\n', encoding="utf-8")
    (project / "README.md").write_text("# X\n", encoding="utf-8")
    responder = _responder(
        "inspect_project",
        {"path": "."},
        plan=plan_json(("افهم المشروع", "inspect_project")),
    )
    platform = make_platform(responder)
    result = await platform.build_runner().run(
        principal=_principal(), goal="ما نوع هذا المشروع؟", workspace_dir=str(project)
    )
    assert result.status is RunStatus.COMPLETED
    events = [
        e.type for e in await platform.audit.list_for_run(tenant_id="t1", run_id=result.run_id)
    ]
    assert AuditEventType.PROJECT_INSPECTED in events


async def test_agent_edit_file_requires_approval_then_completes(
    make_platform, tmp_path: Path
) -> None:
    from arabic_agent.core.enums import ApprovalStatus

    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "code.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")

    responder = _responder(
        "edit_file",
        {"path": "code.py", "old": "VALUE = 1", "new": "VALUE = 2"},
        plan=plan_json(("عدّل الملف", "edit_file")),
    )
    platform = make_platform(responder)
    runner = platform.build_runner()
    result = await runner.run(
        principal=_principal(), goal="غيّر القيمة", workspace_dir=str(workspace)
    )
    # Writing requires approval: the run pauses rather than editing silently.
    assert result.status is RunStatus.WAITING_APPROVAL
    assert result.pending_approval_id
    # The file is untouched until approval.
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"

    approval = await platform.approvals.resolve(
        result.pending_approval_id,
        tenant_id="t1",
        status=ApprovalStatus.APPROVED,
        resolved_by="u1",
    )
    resumed = await runner.resume_after_approval(
        principal=_principal(),
        approval=approval,
        run_id=result.run_id,
        workspace_dir=str(workspace),
    )
    assert resumed.status is RunStatus.COMPLETED
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"


async def test_secret_in_document_emits_security_event(make_platform, tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "config.txt").write_text("api_key = sk-abcdefghijklmnopqrstuvwx\n", encoding="utf-8")
    responder = _responder(
        "inspect_document",
        {"path": "config.txt"},
        plan=plan_json(("افحص الملف", "inspect_document")),
    )
    platform = make_platform(responder)
    result = await platform.build_runner().run(
        principal=_principal(), goal="افحص الإعدادات", workspace_dir=str(docs)
    )
    assert result.status is RunStatus.COMPLETED
    events = [
        e.type for e in await platform.audit.list_for_run(tenant_id="t1", run_id=result.run_id)
    ]
    assert AuditEventType.SECRET_DETECTED in events
    # The secret value must not appear anywhere in the audit log.
    history = await platform.audit.list_for_run(tenant_id="t1", run_id=result.run_id)
    blob = " ".join(str(e.data) + e.message for e in history)
    assert "sk-abcdefghijklmnopqrstuvwx" not in blob
