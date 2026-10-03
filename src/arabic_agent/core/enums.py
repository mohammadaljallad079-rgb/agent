"""Shared enumerations for the platform domain model.

Keeping these in one module avoids circular imports between the tool,
permission, task and agent layers, which all need to agree on these values.
"""

from __future__ import annotations

from enum import StrEnum as _StdStrEnum


class StrEnum(_StdStrEnum):
    """String enum that serialises to its value (JSON/pydantic friendly)."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


class RiskLevel(StrEnum):
    """How dangerous an operation is if executed without scrutiny."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class PermissionEffect(StrEnum):
    """Outcome of a policy decision."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRES_APPROVAL = "requires_approval"


class ErrorClass(StrEnum):
    """Classification of a failure, driving recovery strategy.

    The agent never assumes the first execution succeeded; it classifies the
    error and chooses an appropriate recovery path.
    """

    TRANSIENT = "transient"  # safe to retry (network blips, rate limits)
    TIMEOUT = "timeout"  # retry with backoff or smaller scope
    VALIDATION = "validation"  # bad input; repair args, do not blindly retry
    PERMISSION = "permission"  # policy denied; requires human/approval
    NOT_FOUND = "not_found"  # missing resource; adjust plan
    CONFLICT = "conflict"  # state changed; re-read then act
    RESOURCE = "resource"  # out of quota/space; free resources or abort
    EXTERNAL = "external"  # upstream integration failed
    INTERNAL = "internal"  # bug in our code; do not retry blindly


class RunStatus(StrEnum):
    """Lifecycle of a single agent run (one user goal)."""

    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    """Lifecycle of a single task step."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


class ApprovalStatus(StrEnum):
    """Lifecycle of a human-in-the-loop approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


class MemoryKind(StrEnum):
    """Categories of memory, each with distinct retention/access rules."""

    CONVERSATION = "conversation"
    PREFERENCE = "preference"
    COMPANY = "company"
    TASK = "task"
    KNOWLEDGE = "knowledge"
    WORKFLOW = "workflow"


class ModelCapability(StrEnum):
    """Capabilities a model may advertise, so tasks can be routed."""

    CHAT = "chat"
    TOOL_CALLING = "tool_calling"
    VISION = "vision"
    AUDIO = "audio"
    EMBEDDING = "embedding"
    CODE = "code"


class RuntimeKind(StrEnum):
    """Where a piece of work executes."""

    CLOUD = "cloud"
    LOCAL = "local"


class Locale(StrEnum):
    """Supported UI/content locales. Arabic is first-class, not a translation."""

    AR = "ar"
    EN = "en"


class Direction(StrEnum):
    """Text direction derived from locale."""

    RTL = "rtl"
    LTR = "ltr"
