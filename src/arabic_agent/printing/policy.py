"""Print policy and job budgeting.

The permission layer decides *whether* the agent may print at all; this policy
decides *how far* a single job may go and how often printing may repeat. It is a
small, pure, declarative guard that sits inside the print runtime, before any
spooler call:

* a maximum size for a single document, so a runaway model cannot spool a
  multi-gigabyte file;
* a per-run job budget, so printing cannot become an unbounded paper waste;
* a minimum interval between jobs (anti-runaway);
* the set of content types that may be printed, defaulting to the document
  formats this platform actually produces.

Every refusal is a classified :class:`PlatformError` the agent loop can recover
from, and is auditable.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..core.errors import ResourceExhaustedError, ValidationError

#: Content types printable by default: the formats the document generator
#: produces, plus plain text. A backend may accept more; policy is the gate.
DEFAULT_PRINTABLE_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "text/csv",
        "text/plain",
        "text/markdown",
        "text/html",
        "application/json",
        "image/png",
        "image/jpeg",
    }
)


class PrintPolicy(BaseModel):
    """Declarative bounds for printing."""

    model_config = ConfigDict(frozen=True)

    #: Largest document (bytes) that may be submitted in one job.
    max_document_bytes: int = Field(default=100 * 1024 * 1024, ge=1)
    #: Per-run cap on submitted jobs.
    max_jobs_per_run: int = Field(default=25, ge=1)
    #: Minimum seconds between two jobs (anti-runaway).
    min_job_interval_seconds: float = Field(default=0.0, ge=0.0)
    #: Content types that may be printed.
    allowed_content_types: frozenset[str] = DEFAULT_PRINTABLE_TYPES
    #: Whether a content type must be one of the allowed set. Kept explicit so
    #: the intent is legible; always true in practice.
    restrict_content_types: bool = True

    def check_content_type(self, content_type: str) -> None:
        if not content_type:
            # An unknown type is refused rather than assumed printable.
            raise ValidationError("document content type is required to print")
        if self.restrict_content_types and content_type not in self.allowed_content_types:
            raise ValidationError(
                "document content type is not printable",
                details={"content_type": content_type},
            )

    def check_size(self, size_bytes: int) -> None:
        if size_bytes > self.max_document_bytes:
            raise ResourceExhaustedError(
                "document exceeds the maximum printable size",
                details={"size_bytes": size_bytes, "max": self.max_document_bytes},
            )


class JobBudget:
    """Per-run print-job counter."""

    def __init__(self, policy: PrintPolicy) -> None:
        self._policy = policy
        self._count = 0
        self._last_at: float | None = None

    @property
    def count(self) -> int:
        return self._count

    def consume(self, *, now: float) -> None:
        """Account for one job, enforcing the budget and interval."""
        if self._count >= self._policy.max_jobs_per_run:
            raise ResourceExhaustedError(
                "print job budget exhausted for this run",
                details={"max_jobs_per_run": self._policy.max_jobs_per_run},
            )
        if (
            self._last_at is not None
            and self._policy.min_job_interval_seconds > 0
            and now - self._last_at < self._policy.min_job_interval_seconds
        ):
            raise ResourceExhaustedError(
                "printing too quickly; the minimum interval has not elapsed",
                details={"min_job_interval_seconds": self._policy.min_job_interval_seconds},
            )
        self._count += 1
        self._last_at = now


__all__ = ["DEFAULT_PRINTABLE_TYPES", "JobBudget", "PrintPolicy"]
