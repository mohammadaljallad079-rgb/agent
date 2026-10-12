"""The print runtime: the single object the print tools call.

It bundles a :class:`PrintBackend`, the :class:`PrintPolicy`, a per-run
:class:`JobBudget`, the local data boundary and the audit log, and enforces the
discipline a physical side effect demands:

* the document must resolve inside the filesystem sandbox (a print request can
  never read an arbitrary file);
* the document's size and content type are bounded by policy;
* every job consumes a per-run budget, so printing cannot become a runaway;
* the data-boundary decision is recorded before the spooler is touched, so a
  document that must not leave the machine is never spooled to a network printer
  under a boundary that forbids it;
* every action emits an audit event carrying *metadata only* — a printer id,
  job id, title and size — never the document bytes.

It is created once and shared; per-run state is keyed by ``run_id``. Tools reach
it through ``context.metadata["print_runtime"]``, exactly as local filesystem
tools reach their sandbox, so the executor and agent runner stay unaware of it.
"""

from __future__ import annotations

import time

from ..audit.base import AuditEventType, AuditLog
from ..core.enums import DataBoundary, DataSensitivity
from ..core.errors import PermissionDeniedError, ValidationError
from ..local.boundary import DataBoundaryPolicy
from .base import (
    PrintBackend,
    PrintDocument,
    PrinterInfo,
    PrintJob,
    PrintJobStatus,
    PrintOptions,
)
from .policy import JobBudget, PrintPolicy

#: Printing a document is an outbound physical action; treat the content as
#: internal by default so the boundary decides whether a remote (network)
#: printer is permitted.
_PRINT_SENSITIVITY = DataSensitivity.INTERNAL


class PrintRuntime:
    """Executes print actions under policy, budget, boundary and audit."""

    def __init__(
        self,
        *,
        backend: PrintBackend,
        policy: PrintPolicy,
        audit: AuditLog,
        boundary: DataBoundaryPolicy | None = None,
    ) -> None:
        self._backend = backend
        self._policy = policy
        self._audit = audit
        self._boundary = boundary
        self._budgets: dict[str, JobBudget] = {}
        self._jobs: dict[str, PrintJob] = {}

    @property
    def backend(self) -> PrintBackend:
        return self._backend

    @property
    def policy(self) -> PrintPolicy:
        return self._policy

    @property
    def available(self) -> bool:
        return self._backend.available

    @property
    def unavailable_reason(self) -> str:
        return self._backend.unavailable_reason

    def _budget(self, run_id: str) -> JobBudget:
        budget = self._budgets.get(run_id)
        if budget is None:
            budget = JobBudget(self._policy)
            self._budgets[run_id] = budget
        return budget

    async def list_printers(
        self, *, tenant_id: str, user_id: str, run_id: str
    ) -> list[PrinterInfo]:
        """Enumerate the host's printers, recording the attempt."""
        printers = await self._backend.list_printers()
        await self._audit.record(
            AuditEventType.PRINT_PRINTERS_LISTED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="printers listed",
            data={
                "count": len(printers),
                "backend": self._backend.name,
                "available": self._backend.available,
            },
        )
        return printers

    async def submit(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        document: PrintDocument,
        printer_id: str,
        options: PrintOptions | None = None,
        resolved_path: str | None = None,
    ) -> PrintJob:
        """Submit a print job after policy, budget and boundary checks.

        ``resolved_path`` is the sandbox-resolved file path the tool already
        validated; it is what the backend reads. The caller is responsible for
        resolving the path through the filesystem sandbox — the runtime does not
        have the sandbox and must not guess.
        """
        self._backend.require_available()
        options = options or PrintOptions()
        if not printer_id:
            raise ValidationError("a printer must be selected to print")
        if not resolved_path:
            raise ValidationError("the document path was not resolved for printing")

        size_bytes = _file_size(resolved_path)
        self._policy.check_size(size_bytes)

        decision = None
        if self._boundary is not None:
            decision = self._boundary.evaluate(
                boundary=DataBoundary.LOCAL,
                sensitivity=_PRINT_SENSITIVITY,
            )
            await self._audit.record(
                AuditEventType.PRINT_BOUNDARY_DECISION,
                tenant_id=tenant_id,
                user_id=user_id,
                run_id=run_id,
                message="print data-boundary decision",
                data={
                    "allowed": decision.allowed,
                    "routed_to": decision.routed_to.value,
                    "reason": decision.reason,
                },
            )
            if not decision.allowed:
                raise PermissionDeniedError(
                    "the data boundary forbids printing this document",
                    details={"reason": decision.reason},
                )

        self._budget(run_id).consume(now=time.monotonic())

        effective = document.model_copy(
            update={"path": resolved_path, "title": document.title or _basename(resolved_path)}
        )
        job = await self._backend.submit(
            document=effective, printer_id=printer_id, options=options
        )
        self._jobs[job.id] = job
        await self._audit.record(
            AuditEventType.PRINT_JOB_SUBMITTED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="print job submitted",
            data={
                "job_id": job.id,
                "printer_id": job.printer_id,
                "title": job.title,
                "size_bytes": size_bytes,
                "copies": options.copies,
                "backend": job.backend,
            },
        )
        return job

    async def job_status(
        self, *, tenant_id: str, user_id: str, run_id: str, job_id: str
    ) -> PrintJobStatus:
        return await self._backend.job_status(job_id)

    async def cancel(
        self, *, tenant_id: str, user_id: str, run_id: str, job_id: str, printer_id: str = ""
    ) -> bool:
        cancelled = await self._backend.cancel_job(job_id, printer_id=printer_id)
        await self._audit.record(
            AuditEventType.PRINT_JOB_CANCELLED if cancelled else AuditEventType.PRINT_JOB_FAILED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="print job cancelled" if cancelled else "print job cancellation refused",
            data={"job_id": job_id, "printer_id": printer_id, "cancelled": cancelled},
        )
        return cancelled


def _file_size(path: str) -> int:
    import os

    try:
        return os.path.getsize(path)
    except OSError as exc:
        raise ValidationError(f"could not read the document to print: {exc}") from exc


def _basename(path: str) -> str:
    import os

    return os.path.basename(path)


__all__ = ["PrintRuntime"]
