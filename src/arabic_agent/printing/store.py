"""Persistent print-job metadata.

A submitted print job is a physical side effect the user may want to inspect,
cancel, or reconcile later — possibly after an application restart. The runtime
records each job here so its metadata survives the process, without ever storing
the document bytes: a job record names the printer, title, size, options and
observed status, nothing more.

Two implementations share one contract:

* :class:`InMemoryPrintJobStore` for tests and a purely in-memory build;
* :class:`LocalPrintJobStore` for the local/desktop runtime, backed by the
  embedded SQLite store so a job survives a restart.

Only safe metadata is persisted. The document path is *not* stored (it may be a
sensitive location); the audit log already records the printer, size and title.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from ..core.errors import NotFoundError
from .base import PrintJob, PrintJobStatus, PrintOptions


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PrintJobRecord(BaseModel):
    """Durable, safe metadata about one print job."""

    model_config = ConfigDict(frozen=True)

    id: str
    tenant_id: str
    user_id: str
    run_id: str = ""
    printer_id: str
    title: str = ""
    status: PrintJobStatus = PrintJobStatus.QUEUED
    backend: str = ""
    size_bytes: int = 0
    options: PrintOptions = Field(default_factory=PrintOptions)
    #: Whether the status was read back from the spooler.
    observed: bool = False
    #: Human-readable detail/error from the spooler.
    detail: str = ""
    error: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    def with_status(
        self, status: PrintJobStatus, *, detail: str = "", error: str = ""
    ) -> PrintJobRecord:
        return self.model_copy(
            update={
                "status": status,
                "detail": detail or self.detail,
                "error": error,
                "observed": True,
                "updated_at": _utcnow(),
            }
        )

    @classmethod
    def from_job(
        cls,
        job: PrintJob,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        size_bytes: int,
        options: PrintOptions,
    ) -> PrintJobRecord:
        return cls(
            id=job.id,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            printer_id=job.printer_id,
            title=job.title,
            status=job.status,
            backend=job.backend,
            size_bytes=size_bytes,
            options=options,
            observed=job.observed,
            detail=job.detail,
            error=job.error,
        )


class PrintJobStore(ABC):
    """Persistence contract for print-job metadata."""

    @abstractmethod
    async def add(self, record: PrintJobRecord) -> PrintJobRecord: ...

    @abstractmethod
    async def get(self, job_id: str, *, tenant_id: str) -> PrintJobRecord: ...

    @abstractmethod
    async def update(self, record: PrintJobRecord) -> PrintJobRecord: ...

    @abstractmethod
    async def list_for_run(self, *, tenant_id: str, run_id: str) -> list[PrintJobRecord]: ...

    @abstractmethod
    async def list_recent(self, *, tenant_id: str, limit: int = 50) -> list[PrintJobRecord]: ...

    async def aclose(self) -> None:
        return None


class InMemoryPrintJobStore(PrintJobStore):
    """Process-local print-job store for tests and a non-durable build."""

    def __init__(self) -> None:
        self._items: dict[str, PrintJobRecord] = {}

    async def add(self, record: PrintJobRecord) -> PrintJobRecord:
        self._items[record.id] = record
        return record

    async def get(self, job_id: str, *, tenant_id: str) -> PrintJobRecord:
        record = self._items.get(job_id)
        if record is None or record.tenant_id != tenant_id:
            raise NotFoundError("print job not found", details={"job_id": job_id})
        return record

    async def update(self, record: PrintJobRecord) -> PrintJobRecord:
        if record.id not in self._items:
            raise NotFoundError("print job not found", details={"job_id": record.id})
        self._items[record.id] = record
        return record

    async def list_for_run(self, *, tenant_id: str, run_id: str) -> list[PrintJobRecord]:
        return sorted(
            (r for r in self._items.values() if r.tenant_id == tenant_id and r.run_id == run_id),
            key=lambda r: r.created_at,
        )

    async def list_recent(self, *, tenant_id: str, limit: int = 50) -> list[PrintJobRecord]:
        records = [r for r in self._items.values() if r.tenant_id == tenant_id]
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records[:limit]


__all__ = [
    "InMemoryPrintJobStore",
    "PrintJobRecord",
    "PrintJobStore",
]
