"""Error taxonomy for the platform.

Every failure raised inside a tool or integration is wrapped in a
:class:`PlatformError` (or a subclass) carrying an :class:`ErrorClass`. The
agent's recovery loop dispatches on that classification instead of guessing
from strings, which is what makes retry/repair/replan decisions testable.
"""

from __future__ import annotations

from typing import Any

from .enums import ErrorClass


class PlatformError(Exception):
    """Base error for all platform failures."""

    #: Default classification; subclasses override.
    error_class: ErrorClass = ErrorClass.INTERNAL
    #: Whether a naive retry (same args) could plausibly succeed.
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        error_class: ErrorClass | None = None,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if error_class is not None:
            self.error_class = error_class
        if retryable is not None:
            self.retryable = retryable
        self.details: dict[str, Any] = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": type(self).__name__,
            "message": self.message,
            "error_class": self.error_class.value,
            "retryable": self.retryable,
            "details": self.details,
        }


class ConfigurationError(PlatformError):
    """Invalid or missing configuration (fail fast at startup)."""

    error_class = ErrorClass.INTERNAL
    retryable = False


class ValidationError(PlatformError):
    """Input failed validation. Retrying unchanged will not help."""

    error_class = ErrorClass.VALIDATION
    retryable = False


class TransientError(PlatformError):
    """Temporary failure (network, rate limit). Safe to retry."""

    error_class = ErrorClass.TRANSIENT
    retryable = True


class TimeoutError_(PlatformError):
    """Operation exceeded its deadline."""

    error_class = ErrorClass.TIMEOUT
    retryable = True


class NotFoundError(PlatformError):
    """A referenced resource does not exist."""

    error_class = ErrorClass.NOT_FOUND
    retryable = False


class ConflictError(PlatformError):
    """State changed underneath us (optimistic concurrency, duplicate)."""

    error_class = ErrorClass.CONFLICT
    retryable = True


class PermissionDeniedError(PlatformError):
    """Policy denied the operation or an approval was refused."""

    error_class = ErrorClass.PERMISSION
    retryable = False


class ApprovalRequiredError(PlatformError):
    """The operation is permitted only after explicit human approval."""

    error_class = ErrorClass.PERMISSION
    retryable = False

    def __init__(self, message: str, *, approval_id: str | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.approval_id = approval_id
        if approval_id is not None:
            self.details["approval_id"] = approval_id


class ResourceExhaustedError(PlatformError):
    """Out of budget: tokens, time, disk, quota, retries."""

    error_class = ErrorClass.RESOURCE
    retryable = False


class ExternalServiceError(PlatformError):
    """An upstream integration or model provider failed."""

    error_class = ErrorClass.EXTERNAL
    retryable = True


class ToolExecutionError(PlatformError):
    """A tool raised while executing its work."""

    error_class = ErrorClass.INTERNAL
    retryable = False
