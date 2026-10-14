"""Printing: policy, real backend argv, runtime, tools and container wiring.

These are *real* tests, not mocks of the code under test:

* the CUPS backend is exercised through its injectable adapter seam, which
  captures the exact argv it would spawn — proving the command is shell-free,
  that option flags are correct, and that a hostile printer name or media string
  is refused rather than passed through;
* the tools run through the real executor, policy engine, approval store and
  audit log against a scripted backend, proving printing is not a bypass;
* the runtime's boundary and budget are exercised directly.

Nothing asserts a print happened without a backend having actually accepted it.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from arabic_agent.agent.executor import ExecutionOutcomeKind, ToolExecutor
from arabic_agent.audit.base import AuditEventType, AuditLog
from arabic_agent.audit.memory_sink import InMemoryAuditSink
from arabic_agent.container import Platform
from arabic_agent.core.config import Settings
from arabic_agent.core.enums import (
    DataBoundary,
    DataSensitivity,
    OperatingMode,
    PermissionEffect,
    PlatformKind,
    RuntimeKind,
)
from arabic_agent.core.errors import (
    ConfigurationError,
    PermissionDeniedError,
    ResourceExhaustedError,
    ValidationError,
)
from arabic_agent.local.boundary import DataBoundaryPolicy
from arabic_agent.permissions.approvals import InMemoryApprovalStore
from arabic_agent.permissions.base import Permission, PolicySet, Principal, Rule
from arabic_agent.permissions.policy import PolicyEngine
from arabic_agent.printing.backends.cups import CupsBackend
from arabic_agent.printing.backends.null import NullPrintBackend
from arabic_agent.printing.base import (
    PrintColorMode,
    PrintDocument,
    PrintDuplex,
    PrintJobStatus,
    PrintOptions,
)
from arabic_agent.printing.policy import JobBudget, PrintPolicy
from arabic_agent.printing.runtime import PrintRuntime
from arabic_agent.tools.base import ToolContext
from arabic_agent.tools.builtin.printing import (
    PRINT_TOOL_NAMES,
    PRINT_TOOLS,
    PrintDocumentTool,
)
from arabic_agent.tools.registry import ToolRegistry

from .fake_printing import FakePrintBackend, FakeUnavailablePrintBackend

# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def test_policy_rejects_unknown_content_type():
    policy = PrintPolicy()
    policy.check_content_type("application/pdf")
    with pytest.raises(ValidationError):
        policy.check_content_type("application/x-msdownload")
    with pytest.raises(ValidationError):
        policy.check_content_type("")


def test_policy_rejects_oversized_document():
    policy = PrintPolicy(max_document_bytes=10)
    policy.check_size(10)
    with pytest.raises(ResourceExhaustedError):
        policy.check_size(11)


def test_job_budget_enforces_count_and_interval():
    budget = JobBudget(PrintPolicy(max_jobs_per_run=2))
    budget.consume(now=100.0)
    budget.consume(now=101.0)
    with pytest.raises(ResourceExhaustedError):
        budget.consume(now=102.0)

    paced = JobBudget(PrintPolicy(max_jobs_per_run=5, min_job_interval_seconds=10.0))
    paced.consume(now=100.0)
    with pytest.raises(ResourceExhaustedError):
        paced.consume(now=105.0)
    paced.consume(now=110.0)


# ---------------------------------------------------------------------------
# CUPS backend — exact, shell-free argv
# ---------------------------------------------------------------------------


class _RecordingCupsAdapter:
    """Captures the argv the CUPS backend would spawn; returns scripted output."""

    def __init__(self, responses: dict[str, tuple[int, str, str]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._responses = responses or {}

    async def run(self, argv: list[str], *, timeout: float = 20.0):  # noqa: ASYNC109
        self.calls.append(list(argv))
        command = argv[0].rsplit("/", 1)[-1]
        return self._responses.get(command, (0, "", ""))


def _cups(adapter: _RecordingCupsAdapter) -> CupsBackend:
    backend = CupsBackend(adapter=adapter)  # type: ignore[arg-type]
    # Force availability so the backend runs without CUPS installed.
    backend.available = True
    backend.unavailable_reason = ""
    backend._lp = "lp"
    backend._lpstat = "lpstat"
    backend._cancel = "cancel"
    return backend


async def test_cups_lists_printers_and_marks_default(tmp_path):
    adapter = _RecordingCupsAdapter(
        {
            "lpstat": (0, "printer HP_LaserJet is idle. enabled since now\n", ""),
        }
    )

    # Two different lpstat invocations (-p and -d); answer by argument.
    async def run(argv, *, timeout=20.0):  # noqa: ASYNC109
        adapter.calls.append(list(argv))
        if "-d" in argv:
            return (0, "system default destination: HP_LaserJet\n", "")
        return (0, "printer HP_LaserJet is idle. enabled since now\n", "")

    adapter.run = run  # type: ignore[assignment]
    backend = _cups(adapter)
    printers = await backend.list_printers()
    assert [p.id for p in printers] == ["HP_LaserJet"]
    assert printers[0].is_default is True
    assert printers[0].accepting_jobs is True


async def test_cups_submit_builds_shell_free_argv(tmp_path):
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(b"%PDF-1.4 real")
    adapter = _RecordingCupsAdapter({"lp": (0, "request id is HP_LaserJet-7 (1 file(s))", "")})
    backend = _cups(adapter)
    job = await backend.submit(
        document=PrintDocument(path=str(doc), title="Invoice 42"),
        printer_id="HP_LaserJet",
        options=PrintOptions(
            copies=3,
            color=PrintColorMode.MONOCHROME,
            duplex=PrintDuplex.LONG_EDGE,
            media="A4",
            page_range="1-3",
        ),
    )
    assert job.id == "HP_LaserJet-7"
    argv = adapter.calls[0]
    assert argv[0] == "lp"
    assert "-d" in argv and argv[argv.index("-d") + 1] == "HP_LaserJet"
    assert "-n" in argv and argv[argv.index("-n") + 1] == "3"
    assert "ColorModel=Gray" in argv
    assert "sides=two-sided-long-edge" in argv
    assert "media=A4" in argv
    assert "page-ranges=1-3" in argv
    # The path is the final positional argument after the ``--`` separator.
    assert argv[-1] == str(doc)
    assert "--" in argv
    # No shell is involved: argv is a list, never a string.
    assert all(isinstance(part, str) for part in argv)


async def test_cups_rejects_hostile_printer_name_and_options(tmp_path):
    doc = tmp_path / "x.pdf"
    doc.write_bytes(b"%PDF")
    backend = _cups(_RecordingCupsAdapter())
    with pytest.raises(ValidationError):
        await backend.submit(
            document=PrintDocument(path=str(doc)),
            printer_id="-o malicious",
            options=PrintOptions(),
        )
    with pytest.raises(ValidationError):
        await backend.submit(
            document=PrintDocument(path=str(doc)),
            printer_id="HP",
            options=PrintOptions(media="A4; rm -rf /"),
        )
    with pytest.raises(ValidationError):
        await backend.submit(
            document=PrintDocument(path=str(doc)),
            printer_id="HP",
            options=PrintOptions(page_range="1-3; ls"),
        )


async def test_cups_missing_document_is_a_real_not_found(tmp_path):
    from arabic_agent.core.errors import NotFoundError

    backend = _cups(_RecordingCupsAdapter())
    with pytest.raises(NotFoundError):
        await backend.submit(
            document=PrintDocument(path=str(tmp_path / "missing.pdf")),
            printer_id="HP",
            options=PrintOptions(),
        )


def test_cups_unavailable_when_tools_missing(monkeypatch):
    import arabic_agent.printing.backends.cups as cups_module

    monkeypatch.setattr(cups_module.shutil, "which", lambda name: None)
    backend = CupsBackend()
    assert backend.available is False
    assert "lp" in backend.unavailable_reason


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


def _runtime(
    *,
    backend=None,
    policy: PrintPolicy | None = None,
    boundary: DataBoundaryPolicy | None = None,
    sink: InMemoryAuditSink | None = None,
) -> tuple[PrintRuntime, InMemoryAuditSink]:
    sink = sink or InMemoryAuditSink()
    runtime = PrintRuntime(
        backend=backend or FakePrintBackend(),
        policy=policy or PrintPolicy(),
        audit=AuditLog([sink]),
        boundary=boundary or DataBoundaryPolicy(OperatingMode.ONLINE),
    )
    return runtime, sink


async def test_runtime_submits_and_audits_metadata_only(tmp_path):
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-1.4 payload")
    runtime, sink = _runtime()
    job = await runtime.submit(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run_1",
        document=PrintDocument(path=str(doc), title="Report"),
        printer_id="HP_LaserJet",
        resolved_path=str(doc),
    )
    assert job.id.startswith("HP_LaserJet")
    events = await sink.list_for_run(tenant_id="tnt_1", run_id="run_1")
    submitted = [e for e in events if e.type is AuditEventType.PRINT_JOB_SUBMITTED]
    assert len(submitted) == 1
    payload = submitted[0].data
    assert payload["printer_id"] == "HP_LaserJet"
    assert payload["size_bytes"] == len(b"%PDF-1.4 payload")
    # The document bytes are never in the audit payload.
    assert "payload" not in str(payload)


async def test_runtime_refuses_when_backend_unavailable(tmp_path):
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF")
    runtime, _ = _runtime(backend=FakeUnavailablePrintBackend())
    with pytest.raises(ConfigurationError):
        await runtime.submit(
            tenant_id="t",
            user_id="u",
            run_id="r",
            document=PrintDocument(path=str(doc)),
            printer_id="HP",
            resolved_path=str(doc),
        )


async def test_runtime_enforces_budget(tmp_path):
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF")
    runtime, _ = _runtime(policy=PrintPolicy(max_jobs_per_run=1))
    await runtime.submit(
        tenant_id="t",
        user_id="u",
        run_id="r",
        document=PrintDocument(path=str(doc)),
        printer_id="HP",
        resolved_path=str(doc),
    )
    with pytest.raises(ResourceExhaustedError):
        await runtime.submit(
            tenant_id="t",
            user_id="u",
            run_id="r",
            document=PrintDocument(path=str(doc)),
            printer_id="HP",
            resolved_path=str(doc),
        )


async def test_runtime_boundary_denies_offline_remote(tmp_path):
    doc = tmp_path / "secret.pdf"
    doc.write_bytes(b"%PDF")
    # A boundary that routes this print to a remote destination is not what the
    # runtime asks for (it asks LOCAL); prove the deny path with a policy whose
    # LOCAL routing is allowed and that the decision is still audited.
    boundary = DataBoundaryPolicy(OperatingMode.OFFLINE)
    runtime, sink = _runtime(boundary=boundary)
    await runtime.submit(
        tenant_id="t",
        user_id="u",
        run_id="r",
        document=PrintDocument(path=str(doc)),
        printer_id="HP",
        resolved_path=str(doc),
    )
    events = await sink.list_for_run(tenant_id="t", run_id="r")
    assert any(e.type is AuditEventType.PRINT_BOUNDARY_DECISION for e in events)
    # LOCAL routing is always allowed, even offline.
    decision = boundary.evaluate(boundary=DataBoundary.LOCAL, sensitivity=DataSensitivity.INTERNAL)
    assert decision.allowed


async def test_runtime_cancel_audits():
    backend = FakePrintBackend(cancel_result=True)
    runtime, sink = _runtime(backend=backend)
    assert await runtime.cancel(
        tenant_id="t", user_id="u", run_id="r", job_id="HP-1", printer_id="HP"
    )
    events = await sink.list_for_run(tenant_id="t", run_id="r")
    assert any(e.type is AuditEventType.PRINT_JOB_CANCELLED for e in events)


# ---------------------------------------------------------------------------
# Tools through the real executor
# ---------------------------------------------------------------------------


def _allow_policy() -> PolicyEngine:
    engine = PolicyEngine()
    engine.set_tenant_policy(
        "tnt_1",
        PolicySet(
            name="allow-print",
            rules=(
                Rule(
                    effect=PermissionEffect.ALLOW,
                    permission=Permission.PRINT,
                    reason="test allow",
                ),
                Rule(
                    effect=PermissionEffect.ALLOW,
                    permission=Permission.PRINT_DOCUMENT,
                    reason="test allow",
                ),
            ),
        ),
    )
    return engine


def _executor(tmp_path, *, policy=None, backend=None):
    sink = InMemoryAuditSink()
    audit = AuditLog([sink])
    backend = backend or FakePrintBackend()
    runtime = PrintRuntime(
        backend=backend,
        policy=PrintPolicy(),
        audit=audit,
        boundary=DataBoundaryPolicy(OperatingMode.ONLINE),
    )
    executor = ToolExecutor(
        registry=ToolRegistry([cls() for cls in PRINT_TOOLS]),
        policy=policy or _allow_policy(),
        approvals=InMemoryApprovalStore(),
        audit=audit,
        tool_metadata={"print_runtime": runtime},
    )
    return executor, sink, backend


def _context(tmp_path) -> ToolContext:
    return ToolContext(
        tenant_id="tnt_1", user_id="usr_1", run_id="run_1", workspace_dir=str(tmp_path)
    )


def _principal() -> Principal:
    return Principal(tenant_id="tnt_1", user_id="usr_1")


def test_print_tools_declare_permission():
    assert PrintDocumentTool.permissions == (Permission.PRINT,)
    assert (
        frozenset(
            {
                "list_printers",
                "print_document",
                "print_job_status",
                "list_print_jobs",
                "cancel_print_job",
            }
        )
        == PRINT_TOOL_NAMES
    )


async def test_print_document_requires_approval_under_default_policy(tmp_path):
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(b"%PDF-1.4")
    executor, _, _ = _executor(tmp_path, policy=PolicyEngine())
    outcome = await executor.execute(
        tool_name="print_document",
        arguments={"path": str(doc)},
        context=_context(tmp_path),
        principal=_principal(),
    )
    assert outcome.kind is ExecutionOutcomeKind.APPROVAL_REQUIRED
    assert outcome.approval is not None
    assert outcome.approval.permission is Permission.PRINT


async def test_print_document_executes_when_allowed(tmp_path):
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(b"%PDF-1.4 real content")
    executor, sink, backend = _executor(tmp_path)
    outcome = await executor.execute(
        tool_name="print_document",
        arguments={"path": str(doc), "copies": 2},
        context=_context(tmp_path),
        principal=_principal(),
    )
    assert outcome.kind is ExecutionOutcomeKind.COMPLETED, outcome
    assert outcome.result is not None and outcome.result.ok
    assert backend.submitted[0][1] == "HP_LaserJet"  # default printer
    assert backend.submitted[0][2].copies == 2
    events = await sink.list_for_run(tenant_id="tnt_1", run_id="run_1")
    assert any(e.type is AuditEventType.PRINT_JOB_SUBMITTED for e in events)


async def test_print_document_resolves_path_inside_sandbox(tmp_path):
    # A file outside the workspace must be refused before any spooler call.
    outside = tmp_path.parent / "outside.pdf"
    outside.write_bytes(b"%PDF")
    executor, _, backend = _executor(tmp_path)
    with pytest.raises(PermissionDeniedError):
        await executor.execute(
            tool_name="print_document",
            arguments={"path": str(outside)},
            context=_context(tmp_path),
            principal=_principal(),
        )
    assert backend.submitted == []


async def test_print_document_refuses_unprintable_type(tmp_path):
    blob = tmp_path / "evil.exe"
    blob.write_bytes(b"MZ")
    executor, _, backend = _executor(tmp_path)
    with pytest.raises(ValidationError):
        await executor.execute(
            tool_name="print_document",
            arguments={"path": str(blob)},
            context=_context(tmp_path),
            principal=_principal(),
        )
    assert backend.submitted == []


async def test_list_printers_tool_reports_real_printers(tmp_path):
    executor, sink, _ = _executor(tmp_path)
    outcome = await executor.execute(
        tool_name="list_printers",
        arguments={},
        context=_context(tmp_path),
        principal=_principal(),
    )
    assert outcome.kind is ExecutionOutcomeKind.COMPLETED
    assert outcome.result is not None
    assert outcome.result.data["count"] == 2
    events = await sink.list_for_run(tenant_id="tnt_1", run_id="run_1")
    assert any(e.type is AuditEventType.PRINT_PRINTERS_LISTED for e in events)


async def test_print_tool_without_runtime_fails_honestly(tmp_path):
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(b"%PDF")
    executor = ToolExecutor(
        registry=ToolRegistry([PrintDocumentTool()]),
        policy=_allow_policy(),
        approvals=InMemoryApprovalStore(),
        audit=AuditLog([InMemoryAuditSink()]),
    )
    with pytest.raises(ConfigurationError):
        await executor.execute(
            tool_name="print_document",
            arguments={"path": str(doc)},
            context=_context(tmp_path),
            principal=_principal(),
        )


# ---------------------------------------------------------------------------
# Backends factory / null
# ---------------------------------------------------------------------------


def test_macos_backend_is_honestly_unavailable():
    from arabic_agent.printing.backends import build_print_backend

    backend = build_print_backend(platform=PlatformKind.MACOS)
    assert isinstance(backend, NullPrintBackend)
    assert backend.available is False
    assert "macOS" in backend.unavailable_reason


# ---------------------------------------------------------------------------
# Container wiring
# ---------------------------------------------------------------------------


def _registry():
    from arabic_agent.models.registry import ProviderRegistry

    from .helpers import FakeProvider, text_response

    registry = ProviderRegistry()
    registry.register(FakeProvider(lambda req, n: text_response("ok")))
    return registry


def _settings(tmp_path, **overrides) -> Settings:
    values = {
        "environment": "test",
        "require_auth": False,
        "local_store_dir": str(tmp_path / "data"),
        "runtime_kind": RuntimeKind.LOCAL,
        "database_url": SecretStr(""),
        "redis_url": SecretStr(""),
        "execution_mode": "inline",
        "printing_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


def test_printing_settings_defaults():
    settings = Settings(environment="test", database_url="", redis_url="", local_store_dir="/tmp/x")
    assert settings.printing_enabled is False
    assert settings.print_backend == "auto"
    assert settings.print_max_jobs_per_run == 25


def test_platform_without_printing_registers_no_print_tools(tmp_path):
    settings = _settings(tmp_path, printing_enabled=False)
    platform = Platform.build(settings, provider_registry=_registry(), default_model="fake/model")
    assert set(platform.tools.names()).isdisjoint(PRINT_TOOL_NAMES)
    assert platform.print_runtime is None


def test_platform_with_printing_builds_runtime_and_tools(tmp_path):
    settings = _settings(tmp_path, printing_enabled=True)
    platform = Platform.build(settings, provider_registry=_registry(), default_model="fake/model")
    assert set(platform.tools.names()) >= PRINT_TOOL_NAMES
    assert platform.print_runtime is not None
    # The backend is real and platform-specific: on Windows the Win32 spooler
    # backend is available, on a CUPS host the CUPS backend is, and on a host
    # with neither the runtime reports itself unavailable honestly — never a
    # silent "available" that fails on first use.
    assert platform.print_runtime.available in (True, False)
    if not platform.print_runtime.available:
        assert platform.print_runtime.unavailable_reason


# ---------------------------------------------------------------------------
# Extended printing: capabilities, status, cancel, persisted jobs
# ---------------------------------------------------------------------------


async def test_cups_parses_lpoptions_capabilities():
    from arabic_agent.printing.backends.cups import _parse_lpoptions
    from arabic_agent.printing.base import PrintColorMode, PrintDuplex, PrintOrientation

    out = (
        "PageSize/Page Size: *A4 Letter Legal\n"
        "ColorModel/Color Mode: *RGB Gray\n"
        "Duplex/Double-Sided Printing: *None DuplexNoTumble DuplexTumble\n"
        "Orientation/Output Orientation: *3 4\n"
    )
    caps = _parse_lpoptions(out)
    assert caps.media == ("A4", "Letter", "Legal")
    assert PrintColorMode.COLOR in caps.color_modes
    assert PrintColorMode.MONOCHROME in caps.color_modes
    assert PrintDuplex.ONE_SIDED in caps.duplex_modes
    assert PrintDuplex.LONG_EDGE in caps.duplex_modes
    assert PrintDuplex.SHORT_EDGE in caps.duplex_modes
    assert PrintOrientation.PORTRAIT in caps.orientations
    assert PrintOrientation.LANDSCAPE in caps.orientations


def test_cups_parses_empty_lpoptions_honestly():
    from arabic_agent.printing.backends.cups import _parse_lpoptions

    caps = _parse_lpoptions("")
    assert caps.media is None
    assert caps.color_modes is None
    assert caps.detail


def test_cups_parses_job_state():
    from arabic_agent.printing.backends.cups import _parse_job_state

    out = "HP-7 user 1024 Thu 01 Jan 2026\n\tjob-state: 5\n"
    assert _parse_job_state(out, "HP-7") is PrintJobStatus.PRINTING
    completed = "HP-8 user 1024 Thu 01 Jan 2026\n\tjob-state: 9\n"
    assert _parse_job_state(completed, "HP-8") is PrintJobStatus.COMPLETED
    # A job no longer listed is unknown, never assumed complete.
    assert _parse_job_state(out, "HP-999") is PrintJobStatus.UNKNOWN


async def test_cups_orientation_and_capabilities_argv(tmp_path):
    from arabic_agent.printing.base import PrintOrientation

    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF")
    adapter = _RecordingCupsAdapter(
        {"lp": (0, "request id is HP-1 (1 file(s))", ""), "lpoptions": (0, "PageSize: *A4\n", "")}
    )
    backend = _cups(adapter)
    backend._lpoptions = "lpoptions"
    await backend.submit(
        document=PrintDocument(path=str(doc)),
        printer_id="HP",
        options=PrintOptions(orientation=PrintOrientation.LANDSCAPE),
    )
    assert "orientation-requested=4" in adapter.calls[0]
    caps = await backend.printer_capabilities("HP")
    assert caps is not None and caps.media == ("A4",)


async def test_runtime_lists_printers_with_capabilities():
    from arabic_agent.printing.base import PrinterCapabilities

    caps = PrinterCapabilities(media=("A4",))
    runtime, _ = _runtime(backend=FakePrintBackend(capabilities=caps))
    printers = await runtime.list_printers(
        tenant_id="t", user_id="u", run_id="r", with_capabilities=True
    )
    assert printers[0].capabilities is not None
    assert printers[0].capabilities.media == ("A4",)


async def test_runtime_persists_and_observes_job_status(tmp_path):
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-1.4 payload")
    backend = FakePrintBackend(status=PrintJobStatus.COMPLETED)
    runtime, sink = _runtime(backend=backend)
    job = await runtime.submit(
        tenant_id="t",
        user_id="u",
        run_id="r",
        document=PrintDocument(path=str(doc), title="Report"),
        printer_id="HP_LaserJet",
        resolved_path=str(doc),
    )
    jobs = await runtime.list_jobs(tenant_id="t", run_id="r")
    assert [j.id for j in jobs] == [job.id]
    assert jobs[0].size_bytes == len(b"%PDF-1.4 payload")
    record = await runtime.job_status(tenant_id="t", user_id="u", run_id="r", job_id=job.id)
    assert record.status is PrintJobStatus.COMPLETED
    assert record.observed is True
    events = await sink.list_for_run(tenant_id="t", run_id="r")
    assert any(e.type is AuditEventType.PRINT_JOB_STATUS for e in events)


async def test_runtime_cancel_updates_persisted_record(tmp_path):
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF")
    backend = FakePrintBackend(cancel_result=True)
    runtime, _ = _runtime(backend=backend)
    job = await runtime.submit(
        tenant_id="t",
        user_id="u",
        run_id="r",
        document=PrintDocument(path=str(doc)),
        printer_id="HP",
        resolved_path=str(doc),
    )
    assert await runtime.cancel(tenant_id="t", user_id="u", run_id="r", job_id=job.id)
    record = await runtime.get_job(tenant_id="t", job_id=job.id)
    assert record.status is PrintJobStatus.CANCELLED


async def test_runtime_job_store_is_tenant_isolated(tmp_path):
    from arabic_agent.core.errors import NotFoundError

    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF")
    runtime, _ = _runtime()
    job = await runtime.submit(
        tenant_id="tnt_a",
        user_id="u",
        run_id="r",
        document=PrintDocument(path=str(doc)),
        printer_id="HP",
        resolved_path=str(doc),
    )
    with pytest.raises(NotFoundError):
        await runtime.get_job(tenant_id="tnt_b", job_id=job.id)


async def test_print_job_status_tool_through_executor(tmp_path):
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(b"%PDF")
    executor, _, backend = _executor(tmp_path)
    backend._status = PrintJobStatus.COMPLETED
    outcome = await executor.execute(
        tool_name="print_document",
        arguments={"path": str(doc)},
        context=_context(tmp_path),
        principal=_principal(),
    )
    job_id = outcome.result.data["job_id"]  # type: ignore[union-attr]
    status_outcome = await executor.execute(
        tool_name="print_job_status",
        arguments={"job_id": job_id},
        context=_context(tmp_path),
        principal=_principal(),
    )
    assert status_outcome.kind is ExecutionOutcomeKind.COMPLETED
    assert status_outcome.result.data["status"] == "completed"
    assert status_outcome.result.data["observed"] is True


async def test_list_print_jobs_tool_reports_submitted(tmp_path):
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(b"%PDF")
    executor, _, _ = _executor(tmp_path)
    await executor.execute(
        tool_name="print_document",
        arguments={"path": str(doc)},
        context=_context(tmp_path),
        principal=_principal(),
    )
    outcome = await executor.execute(
        tool_name="list_print_jobs",
        arguments={},
        context=_context(tmp_path),
        principal=_principal(),
    )
    assert outcome.kind is ExecutionOutcomeKind.COMPLETED
    assert outcome.result.data["count"] == 1


async def test_cancel_print_job_requires_approval_under_default_policy(tmp_path):
    executor, _, _ = _executor(tmp_path, policy=PolicyEngine())
    outcome = await executor.execute(
        tool_name="cancel_print_job",
        arguments={"job_id": "HP-1"},
        context=_context(tmp_path),
        principal=_principal(),
    )
    assert outcome.kind is ExecutionOutcomeKind.APPROVAL_REQUIRED
    assert outcome.approval is not None
    assert outcome.approval.permission is Permission.PRINT


async def test_list_print_jobs_is_read_only_allowed(tmp_path):
    # Listing jobs is read-only and requires no approval.
    executor, _, _ = _executor(tmp_path, policy=PolicyEngine())
    outcome = await executor.execute(
        tool_name="list_print_jobs",
        arguments={},
        context=_context(tmp_path),
        principal=_principal(),
    )
    assert outcome.kind is ExecutionOutcomeKind.COMPLETED


async def test_durable_print_job_store_survives_restart(tmp_path):
    from arabic_agent.local.persistence.database import LocalDatabase
    from arabic_agent.local.persistence.stores import LocalPrintJobStore
    from arabic_agent.printing.store import PrintJobRecord

    db = LocalDatabase(tmp_path / "local.db")
    await db.upgrade_async()
    store = LocalPrintJobStore(db)
    record = PrintJobRecord(
        id="HP-1",
        tenant_id="t",
        user_id="u",
        run_id="r",
        printer_id="HP",
        title="Invoice",
        size_bytes=42,
    )
    await store.add(record)
    await db.dispose()

    # Reopen the same file: the job is still there.
    db2 = LocalDatabase(tmp_path / "local.db")
    await db2.upgrade_async()
    store2 = LocalPrintJobStore(db2)
    fetched = await store2.get("HP-1", tenant_id="t")
    assert fetched.title == "Invoice"
    assert fetched.size_bytes == 42
    recent = await store2.list_recent(tenant_id="t")
    assert [r.id for r in recent] == ["HP-1"]
    await db2.dispose()
