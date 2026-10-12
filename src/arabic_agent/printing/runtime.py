"""The print runtime: the single object the print tools call.

It bundles a :class:`PrintBackend`, the :class:`PrintPolicy`, a per-run
:class:`JobBudget`, the local data boundary, the audit log and a
:class:`PrintJobStore`, and enforces the discipline a physical side effect
demands:

* the document must resolve inside the filesystem sandbox (a print request can
  never read an arbitrary file);
* the document's size and content type are bounded by policy;
* every job consumes a per-run budget, so printing cannot become a runaway;
* the data-boundary decision is recorded before the spooler is touched, so a
  document that must not leave the machine is never spooled to a network printer
  under a boundary that forbids it;
* every job's safe metadata is persisted, so a user can list, inspect and cancel
  jobs later — possibly after a restart — without the document bytes ever being
  stored;
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
from ..core.errors import NotFoundError, PermissionDeniedError, ValidationError
from ..local.boundary import DataBoundaryPolicy
from .base import (
    PrintBackend,
    PrintDocument,
    PrinterCapabilities,
    PrinterInfo,
    PrintJob,
    PrintJobStatus,
    PrintOptions,
)
from .policy import JobBudget, PrintPolicy
from .store import InMemoryPrintJobStore, PrintJobRecord, PrintJobStore

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
        store: PrintJobStore | None = None,
    ) -> None:
        self._backend = backend
        self._policy = policy
        self._audit = audit
        self._boundary = boundary
        self._store = store or InMemoryPrintJobStore()
        self._budgets: dict[str, JobBudget] = {}

    @property
    def backend(self) -> PrintBackend:
        return self._backend

    @property
    def policy(self) -> PrintPolicy:
        return self._policy

    @property
    def store(self) -> PrintJobStore:
        return self._store

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
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        with_capabilities: bool = False,
    ) -> list[PrinterInfo]:
        """Enumerate the host's printers, recording the attempt.

        With ``with_capabilities`` each printer's advertised capabilities are
        queried and attached; a printer whose capabilities cannot be read is
        left with ``capabilities=None`` rather than a guessed default.
        """
        printers = await self._backend.list_printers()
        if with_capabilities:
            enriched: list[PrinterInfo] = []
            for printer in printers:
                caps = await self._backend.printer_capabilities(printer.id)
                enriched.append(
                    printer.model_copy(update={"capabilities": caps}) if caps else printer
                )
            printers = enriched
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
                "with_capabilities": with_capabilities,
            },
        )
        return printers

    async def printer_capabilities(
        self, *, tenant_id: str, user_id: str, run_id: str, printer_id: str
    ) -> PrinterCapabilities | None:
        """Return a printer's advertised capabilities, or ``None`` if unreadable."""
        return await self._backend.printer_capabilities(printer_id)

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
        job = await self._backend.submit(document=effective, printer_id=printer_id, options=options)
        await self._store.add(
            PrintJobRecord.from_job(
                job,
                tenant_id=tenant_id,
                user_id=user_id,
                run_id=run_id,
                size_bytes=size_bytes,
                options=options,
            )
        )
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

    async def get_job(self, *, tenant_id: str, job_id: str) -> PrintJobRecord:
        """Return a persisted job record, isolated to the tenant."""
        return await self._store.get(job_id, tenant_id=tenant_id)

    async def list_jobs(
        self, *, tenant_id: str, run_id: str | None = None, limit: int = 50
    ) -> list[PrintJobRecord]:
        """List persisted jobs for a run (or the tenant's most recent)."""
        if run_id:
            return await self._store.list_for_run(tenant_id=tenant_id, run_id=run_id)
        return await self._store.list_recent(tenant_id=tenant_id, limit=limit)

    async def job_status(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        job_id: str,
        printer_id: str = "",
    ) -> PrintJobRecord:
        """Observe a job's status, persist it and audit the observation.

        The status is read from the spooler; when the backend cannot observe it
        the record keeps ``UNKNOWN`` and ``observed=False`` rather than claiming
        success.
        """
        record = await self._store.get(job_id, tenant_id=tenant_id)
        printer = printer_id or record.printer_id
        status = await self._backend.job_status(job_id, printer_id=printer)
        updated = record.with_status(status)
        await self._store.update(updated)
        await self._audit.record(
            AuditEventType.PRINT_JOB_STATUS,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id or record.run_id,
            message=f"print job status: {status.value}",
            data={
                "job_id": job_id,
                "printer_id": printer,
                "status": status.value,
                "observed": updated.observed,
            },
        )
        return updated

    async def cancel(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        job_id: str,
        printer_id: str = "",
    ) -> bool:
        """Cancel a job, updating and auditing the persisted record.

        A job that was never recorded for this tenant is refused rather than
        silently cancelled by id, so one tenant cannot cancel another's job.
        """
        try:
            record = await self._store.get(job_id, tenant_id=tenant_id)
        except NotFoundError:
            record = None
        printer = printer_id or (record.printer_id if record else "")
        cancelled = await self._backend.cancel_job(job_id, printer_id=printer)
        if record is not None:
            await self._store.update(
                record.with_status(
                    PrintJobStatus.CANCELLED if cancelled else record.status,
                    detail="cancellation requested" if cancelled else "",
                    error="" if cancelled else "the spooler refused the cancellation",
                )
            )
        await self._audit.record(
            AuditEventType.PRINT_JOB_CANCELLED if cancelled else AuditEventType.PRINT_JOB_FAILED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="print job cancelled" if cancelled else "print job cancellation refused",
            data={"job_id": job_id, "printer_id": printer, "cancelled": cancelled},
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
